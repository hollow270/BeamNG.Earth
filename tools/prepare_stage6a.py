"""Create an audited, repository-local development kit using tools/package.py.

Never installs a mod, locates/launches the game, or contacts an OSM service.
Refuses to overwrite an existing kit or write outside this repository.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

REPO=Path(__file__).resolve().parents[1]
REQUIRED_SERVER=('buildingoverlay','buildingoverlaystore','osmbuildings','buildingmanual','buildingbaseline',
                 'buildingtargets','buildingspatial','buildingrender','buildingrefresh','buildingactivation',
                 'buildingretention','buildinglive','earthd','mapdata','buildings','blockworker','netfetch','matslots','protocol')
REQUIRED_LUA=('core','roads','settings','net','buildingactivation','buildingadapter','buildingui','buildinginventory')
REQUIRED_UI=('index.js','EarthMap.vue','EarthBuildingRefresh.vue','buildingRefresh.js','earthSettingsStore.js','EarthSettingsPage.vue','EarthSettingsBody.vue')


def sha(data): return hashlib.sha256(data).hexdigest()


def save(path,value): path.write_text(json.dumps(value,indent=2,sort_keys=True)+'\n',encoding='utf-8')


def no_links(path):
    for part in (path,*path.parents):
        if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()):
            raise RuntimeError('Preparation cannot follow a link/junction: '+str(part))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default=str(REPO/'.venv/stage6a/kit'))
    parser.add_argument('--stage',choices=('6A','6B'),default='6A')
    args=parser.parse_args()
    stage=args.stage; port=47624 if stage=='6B' else 47623
    mod_name='beamng_earth_stage'+stage.lower()+'.zip'
    root=Path(args.output).absolute()
    if '..' in root.parts or not root.resolve().is_relative_to(REPO): parser.error('Output must remain in the repository')
    no_links(root)
    if root.exists(): parser.error('Existing kit retained. Choose a new output directory; no run data is overwritten.')
    root.mkdir(parents=True)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip()
    branch=subprocess.check_output(['git','branch','--show-current'],cwd=REPO,text=True).strip()
    diff=subprocess.check_output(['git','diff','--binary','HEAD'],cwd=REPO)
    version=(REPO/'VERSION').read_text().strip(); package_root='BeamNG.Earth-'+version
    build=root/'build'; build.mkdir()
    subprocess.run([sys.executable,'-B',str(REPO/'tools/package.py'),'--out',str(build)],cwd=REPO,check=True)
    archive=build/(package_root+'.zip')
    source=root/'source'; source.mkdir()
    entries={}
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None: raise RuntimeError('Archive CRC failed')
        for item in z.infolist():
            path=Path(item.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in item.filename or ':' in item.filename or path.parts[0]!=package_root:
                raise RuntimeError('Unsafe package path: '+item.filename)
            if (item.external_attr>>16)&0o170000==0o120000: raise RuntimeError('Package contains a link')
            data=z.read(item)
            rel=Path(*path.parts[1:]).as_posix()
            original=REPO/rel
            if not original.is_file() or sha(original.read_bytes())!=sha(data): raise RuntimeError('Source/package mismatch: '+rel)
            entries[rel]=dict(bytes=len(data),sha256=sha(data))
        z.extractall(source)
        prefix=package_root+'/mod/beamng_earth/'
        with zipfile.ZipFile(root/mod_name,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as mod:
            for name in sorted(z.namelist()):
                if name.startswith(prefix): mod.writestr(name[len(prefix):],z.read(name))
            mod.writestr('_stage6a/diagnostics.lua',(REPO/'tools/stage6a/diagnostics.lua').read_bytes())
    required=['server/'+name+'.py' for name in REQUIRED_SERVER]
    required+=['mod/beamng_earth/lua/ge/extensions/beamngEarth/'+name+'.lua' for name in REQUIRED_LUA]
    required+=['mod/beamng_earth/ui/ui-vue/mods/beamngEarth/'+name for name in REQUIRED_UI]
    required+=['server/earthfast.pyd','server/requirements.txt','VERSION',
               'mod/beamng_earth/levels/beamng_earth/info.json']
    missing=[p for p in required if p not in entries]
    if missing: raise RuntimeError('Required package components missing: '+str(missing))
    if any('/tests/' in p or '.beamng_earth.json' in p or 'building-overlays/' in p or 'activation-' in p for p in entries):
        raise RuntimeError('Private/test/runtime data entered package')
    runtime_source=REPO/'.venv/overlay-tests/python'
    if Path(sys.executable).resolve()!=runtime_source/'python.exe': raise RuntimeError('Use the pinned project-local Python')
    for path in runtime_source.rglob('*'): no_links(path)
    shutil.copytree(runtime_source,root/'runtime',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.log'))
    for p in (root/'runtime').glob('*._pth'):
        if any(Path(line).is_absolute() or '..' in Path(line).parts for line in p.read_text().splitlines() if line and not line.startswith('#')):
            raise RuntimeError('Embedded runtime has an external search path')
    for name in ('serve.py','scenario.py','diagnostics.lua','verify.py'): shutil.copy2(REPO/'tools/stage6a'/name,root/name)
    docs=root/'docs'; docs.mkdir()
    for name in ('LIVE_OSM_BUILDINGS.md','STAGE6A_MANUAL_TEST.md','STAGE6B_UI_FIX.md'):
        p=REPO/'docs'/name
        if p.is_file(): shutil.copy2(p,docs/name)
    sys.path[:0]=[str(source/package_root/'server'),str(root)]
    import scenario,fast,numpy,PIL
    fast.require(fast.MIN_VERSION,'Stage 6A packaged runtime')
    save(root/'SCENARIO.json',scenario.manifest())
    (root/'synthetic-overpass.json').write_bytes(scenario.PAYLOAD)
    save(root/'STAGE6A_WORKSPACE.json',dict(schema='stage6a-workspace-v1',commit=commit,branch=branch,
        package_root=package_root,port=port,stage=stage,source_diff_sha256=sha(diff),synthetic_only=True))
    optional={p:importlib.util.find_spec(p) is not None for p in ('duckdb','fsspec','certifi','truststore')}
    save(root/'package-audit.json',dict(commit=commit,branch=branch,archive=archive.name,stage=stage,game_mod=mod_name,
        archive_sha256=sha(archive.read_bytes()),game_mod_sha256=sha((root/mod_name).read_bytes()),
        files=entries,required_files=required,all_required_present=True,
        native_version=fast._ef.VERSION,native_required=fast.MIN_VERSION,
        python=sys.version,numpy=numpy.__version__,pillow=PIL.__version__,optional_dependencies=optional,
        scope='Windows x64 synthetic legacy-frame development test; no frozen app or real engine test',
        development_sources={str(p.relative_to(REPO)):sha(p.read_bytes()) for p in
            [Path(__file__),REPO/'docs/LIVE_OSM_BUILDINGS.md',REPO/'docs/STAGE6A_MANUAL_TEST.md',REPO/'docs/STAGE6B_UI_FIX.md',
             REPO/'tools/building_ui_render.mjs',REPO/'tools/test_building_ui.mjs',
             *sorted((REPO/'tools/stage6a').glob('*')),*sorted((REPO/'tools/ui-tests').glob('*'))] if p.is_file()},
        range_limits=dict(roads_send_m=384,far_buildings_send_m=384,skyline_send_m=768),
        known_blockers=['Default production-distance live discovery exceeds 128 targets in this fixture.',
                        'Optional online requirements are not installed in the synthetic runtime.',
                        'Real game, collision readiness, UI host, geographic/process-worker startup remain untested.'],
        omissions_compensated_by_kit=['Stage 5B2/6A guides','development fixture/launcher','pinned Python runtime'],
        normal_online_requirements='server/requirements.txt is included; absent optional packages are not needed for this synthetic provider.'))
    launch="""$ErrorActionPreference='Stop'
$stage6Kit=$PSScriptRoot
$stage6Tmp=Join-Path $stage6Kit 'run/tmp'
New-Item -ItemType Directory -Force -Path $stage6Tmp | Out-Null
$env:TMP=$stage6Tmp; $env:TEMP=$stage6Tmp
$ErrorActionPreference='Continue' # Windows PowerShell represents native stderr as error records.
& (Join-Path $stage6Kit 'runtime/python.exe') -B (Join-Path $stage6Kit 'serve.py') --workspace $stage6Kit --synthetic-only --port 47623 2>&1 | Tee-Object -FilePath (Join-Path $stage6Kit 'companion-console.log')
exit $LASTEXITCODE
"""
    (root/'Start-Synthetic-Companion.ps1').write_text(launch.replace('--port 47623','--port '+str(port)),encoding='utf-8')
    print(json.dumps(dict(kit=str(root),archive=str(archive),files=len(entries),required=len(required),native_version=fast._ef.VERSION,optional_dependencies=optional),indent=2))


if __name__=='__main__': main()
