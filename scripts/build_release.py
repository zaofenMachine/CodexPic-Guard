"""Build an allowlisted source release. Never package caches or user settings."""
import hashlib
import json
from pathlib import Path
import tomllib
import zipfile

ROOT=Path(__file__).resolve().parents[1]
VERSION=tomllib.loads((ROOT/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']


def main():
    files=[]
    for name in ('README.md','LICENSE','pyproject.toml','requirements-lock.txt','VALIDATION.md','AGENTS.md','CONTRIBUTING.md','CHANGELOG.md','.gitignore','.gitattributes'):
        p=ROOT/name
        if p.exists():files.append(p)
    files.extend(ROOT.glob('*.ps1'))
    for folder in ('src','tests','scripts'):
        files.extend(p for p in (ROOT/folder).rglob('*.py') if '__pycache__' not in p.parts)
    for suffix in ('*.md','*.json'):
        files.extend((ROOT/'docs').rglob(suffix))
    files=sorted(set(files))
    manifest=[{'path':p.relative_to(ROOT).as_posix(),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
    dist=ROOT/'dist';dist.mkdir(exist_ok=True);target=dist/f'codexpic-guard-{VERSION}.zip'
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p in files:z.write(p,'codexpic-guard/'+p.relative_to(ROOT).as_posix())
        z.writestr('codexpic-guard/MANIFEST.json',json.dumps(manifest,indent=2))
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None
        for row in manifest:
            assert hashlib.sha256(z.read('codexpic-guard/'+row['path'])).hexdigest()==row['sha256']
        assert all(not any(s in name for s in ('.test-data/','.venv/','reports/','guard.json','images.sqlite','admin-token','auth.json','.env')) for name in z.namelist())
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.zip.sha256').write_text(digest+'  '+target.name+'\n',encoding='ascii')
    print(json.dumps({'artifact':str(target),'bytes':target.stat().st_size,'files':len(files)+1,'sha256':digest},indent=2))


if __name__=='__main__':main()
