from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from .config import GuardError, Policy
from .images import ImageBytes, normalize

ID=re.compile(r'^CPIMG-[a-f0-9]{64}$')
SCOPE=re.compile(r'^[a-f0-9]{32}$')


def scope_id(value): return hashlib.sha256(str(value).encode()).hexdigest()[:32]


class Store:
    def __init__(self,root,policy:Policy):
        self.root=Path(root).resolve();self.policy=policy;self.lock=threading.RLock()
        self.blobs=self.root/'blobs';self.blobs.mkdir(parents=True,exist_ok=True)
        self.db=self.root/'images.sqlite'
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS images(id TEXT PRIMARY KEY, mime TEXT NOT NULL, width INTEGER, height INTEGER,
                alpha INTEGER, animated INTEGER, raw_bytes INTEGER, created REAL);
            CREATE TABLE IF NOT EXISTS previews(id TEXT, policy TEXT, file TEXT, mime TEXT, width INTEGER, height INTEGER,
                bytes INTEGER, PRIMARY KEY(id,policy));
            CREATE TABLE IF NOT EXISTS scopes(scope TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS scoped_images(scope TEXT, id TEXT, first_seen REAL, last_seen REAL, pinned INTEGER DEFAULT 0,pending INTEGER DEFAULT 0,
                PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS aliases(hash TEXT PRIMARY KEY,id TEXT);
            CREATE TABLE IF NOT EXISTS grants(scope TEXT,id TEXT,expires REAL,PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY,at REAL,scope TEXT,in_bytes INTEGER,out_bytes INTEGER,
                in_media INTEGER,out_media INTEGER,dropped INTEGER,optimized INTEGER,status INTEGER);
            ''')
            if 'pending' not in {r[1] for r in c.execute('PRAGMA table_info(scoped_images)')}:
                c.execute('ALTER TABLE scoped_images ADD COLUMN pending INTEGER DEFAULT 0')
            if 'sha256' not in {r[1] for r in c.execute('PRAGMA table_info(previews)')}:
                c.execute('ALTER TABLE previews ADD COLUMN sha256 TEXT')
        self.token_file=self.root/'admin-token'
        if not self.token_file.exists():
            try:
                with self.token_file.open('x',encoding='ascii') as f:f.write(secrets.token_urlsafe(32))
            except FileExistsError:pass

    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.db,timeout=30);c.row_factory=sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA busy_timeout=30000')
        try:yield c;c.commit()
        finally:c.close()

    def original_path(self,image_id):
        if not ID.fullmatch(image_id):raise GuardError('invalid_image_id','Invalid image identifier.')
        return self.blobs/(image_id[6:]+'.original')

    def _write(self,path,raw):
        if path.exists():return
        used=sum(p.stat().st_size for p in self.blobs.iterdir() if p.is_file())
        if used+len(raw)>self.policy.cache_limit_bytes:
            raise GuardError('cache_quota_exceeded','CodexPic local cache quota is full. Increase the quota or explicitly clear an unused cache before continuing.',507)
        temp=path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
        try:
            with temp.open('xb') as f:f.write(raw)
            os.replace(temp,path)
        finally:
            if temp.exists():temp.unlink()

    def register(self,source:ImageBytes,scope,mark_pending=False):
        if not SCOPE.fullmatch(scope):raise GuardError('invalid_scope','Invalid image scope.')
        with self.lock,self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            image_id='CPIMG-'+source.sha
            exists=c.execute('SELECT 1 FROM scoped_images WHERE scope=? AND id=?',(scope,image_id)).fetchone() is not None
            row=c.execute('SELECT * FROM images WHERE id=?',(image_id,)).fetchone()
            if not row:
                self._write(self.original_path(image_id),source.data)
                c.execute('INSERT INTO images VALUES(?,?,?,?,?,?,?,?)',(image_id,source.mime,source.width,source.height,int(source.alpha),int(source.animated),len(source.data),time.time()))
                c.execute('INSERT OR REPLACE INTO aliases VALUES(?,?)',(source.sha,image_id))
            now=time.time()
            c.execute('INSERT INTO scoped_images(scope,id,first_seen,last_seen,pending) VALUES(?,?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET last_seen=excluded.last_seen',(scope,image_id,now,now,int(mark_pending)))
            return image_id,exists

    def has_scope(self,scope):
        with self.connect() as c:
            r=c.execute('SELECT requests FROM scopes WHERE scope=?',(scope,)).fetchone();return bool(r and r[0])

    def has_images(self,scope):
        with self.connect() as c:return c.execute('SELECT 1 FROM scoped_images WHERE scope=? LIMIT 1',(scope,)).fetchone() is not None

    def advance(self,scope):
        with self.connect() as c:c.execute('INSERT INTO scopes VALUES(?,1) ON CONFLICT(scope) DO UPDATE SET requests=requests+1',(scope,))

    def get(self,scope,image_id,original=False):
        if not SCOPE.fullmatch(scope) or not ID.fullmatch(image_id):raise GuardError('invalid_image_id','Invalid scope or image identifier.')
        with self.connect() as c:
            row=c.execute('SELECT i.* FROM images i JOIN scoped_images s ON s.id=i.id WHERE s.scope=? AND i.id=?',(scope,image_id)).fetchone()
            if not row:raise GuardError('image_not_in_scope','Image does not belong to this scope.',404)
            preview=c.execute('SELECT * FROM previews WHERE id=? AND policy=?',(image_id,self.policy.cache_key)).fetchone()
        if not original and preview and preview['sha256']:
            if not re.fullmatch(r'[a-f0-9]{64}\.[a-f0-9]{16}\.preview',preview['file']):
                raise GuardError('cache_integrity','Invalid preview cache path.',500)
            raw=(self.blobs/preview['file']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=preview['sha256']:
                raise GuardError('cache_integrity','Preview cache integrity check failed.',500)
            return ImageBytes(raw,preview['mime'],preview['width'],preview['height'],bool(row['alpha']),bool(row['animated']))
        raw=self.original_path(image_id).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=image_id[6:]:raise GuardError('cache_integrity','Image cache integrity check failed.',500)
        source=ImageBytes(raw,row['mime'],row['width'],row['height'],bool(row['alpha']),bool(row['animated']))
        if original:return source
        result=normalize(source,self.policy)
        with self.lock,self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            name=image_id[6:]+'.'+self.policy.cache_key+'.preview'
            self._write(self.blobs/name,result.data)
            c.execute('INSERT OR REPLACE INTO previews(id,policy,file,mime,width,height,bytes,sha256) VALUES(?,?,?,?,?,?,?,?)',(image_id,self.policy.cache_key,name,result.mime,result.width,result.height,len(result.data),result.sha))
            c.execute('INSERT OR IGNORE INTO aliases VALUES(?,?)',(result.sha,image_id))
        return result

    def pinned(self,scope):
        with self.connect() as c:return {r[0] for r in c.execute('SELECT id FROM scoped_images WHERE scope=? AND pinned=1',(scope,))}

    def pending(self,scope):
        with self.connect() as c:return {r[0] for r in c.execute('SELECT id FROM scoped_images WHERE scope=? AND pending=1',(scope,))}

    def delivered(self,scope,ids):
        with self.connect() as c:
            for image_id in ids:c.execute('UPDATE scoped_images SET pending=0 WHERE scope=? AND id=?',(scope,image_id))

    def pin(self,scope,image_id,value):
        self.get(scope,image_id,original=True)
        with self.connect() as c:c.execute('UPDATE scoped_images SET pinned=? WHERE scope=? AND id=?',(int(value),scope,image_id))

    def grant_original(self,scope,image_id):
        self.get(scope,image_id,original=True)
        with self.connect() as c:c.execute('INSERT OR REPLACE INTO grants VALUES(?,?,?)',(scope,image_id,time.time()+600))

    def original_grants(self,scope):
        with self.connect() as c:return {r[0] for r in c.execute('SELECT id FROM grants WHERE scope=? AND expires>?',(scope,time.time()))}

    def consume_grants(self,scope,ids):
        with self.connect() as c:
            for image_id in ids:c.execute('DELETE FROM grants WHERE scope=? AND id=?',(scope,image_id))

    def list_images(self,scope,limit=30):
        if not SCOPE.fullmatch(scope):raise GuardError('invalid_scope','Invalid scope.')
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT s.id,s.pinned,i.mime,i.width,i.height,i.alpha,i.raw_bytes FROM scoped_images s JOIN images i ON s.id=i.id WHERE scope=? ORDER BY last_seen DESC LIMIT ?',(scope,min(100,max(1,limit))))]

    def record(self,scope,in_bytes,out_bytes,stats,status):
        with self.connect() as c:
            c.execute('INSERT INTO metrics(at,scope,in_bytes,out_bytes,in_media,out_media,dropped,optimized,status) VALUES(?,?,?,?,?,?,?,?,?)',(time.time(),scope,in_bytes,out_bytes,stats.get('media_before_bytes',0),stats.get('media_after_bytes',0),stats.get('dropped',0),stats.get('optimized',0),status))

    def status(self):
        with self.connect() as c:
            return dict(images=c.execute('SELECT count(*) FROM images').fetchone()[0],scopes=c.execute('SELECT count(*) FROM scopes').fetchone()[0],requests=c.execute('SELECT count(*) FROM metrics').fetchone()[0],input_body_bytes=c.execute('SELECT coalesce(sum(in_bytes),0) FROM metrics').fetchone()[0],output_body_bytes=c.execute('SELECT coalesce(sum(out_bytes),0) FROM metrics').fetchone()[0],blob_bytes=sum(p.stat().st_size for p in self.blobs.iterdir() if p.is_file()))
