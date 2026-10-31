"""Read-only package/runtime verification, with its report written inside the kit."""
import argparse
import ast
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True)
    args=parser.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    import serve
    root=serve.workspace(args.workspace)
    metadata=json.loads((root/'STAGE6A_WORKSPACE.json').read_text())
    source=serve.safe_child(root,root/'source'/metadata['package_root'])
    audit=json.loads((root/'package-audit.json').read_text())
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    assert digest(root/'build'/audit['archive'])==audit['archive_sha256']
    mod_name=audit.get('game_mod','beamng_earth_stage6a.zip')
    assert digest(serve.safe_child(root,root/mod_name))==audit['game_mod_sha256']
    for name,record in audit['files'].items():
        assert digest(serve.safe_child(root,source/name))==record['sha256'],name
    with zipfile.ZipFile(root/mod_name) as mod:
        assert mod.testzip() is None
        for name in mod.namelist():
            original=root/'diagnostics.lua' if name=='_stage6a/diagnostics.lua' else source/'mod/beamng_earth'/name
            assert mod.read(name)==serve.safe_child(root,original).read_bytes(),name
    sys.path.insert(0,str(source/'server'))
    import fast,earthd
    from PIL import features
    from lupa.luajit21 import LuaRuntime
    fast.require(fast.MIN_VERSION,'Stage 6A runtime audit')
    assert features.check('webp'),'Pillow lacks required WebP support'
    python_files=list((source/'server').rglob('*.py'))
    for p in python_files: ast.parse(p.read_text(encoding='utf-8-sig'),filename=str(p))
    lua=LuaRuntime(unpack_returned_tuples=True)
    compile_lua=lua.eval('function(code,name) local f,e=loadstring(code,name); return f~=nil,e end')
    lua_files=[*source.rglob('*.lua'),root/'diagnostics.lua']
    for p in lua_files:
        ok,error=compile_lua(p.read_text(encoding='utf-8-sig'),'@'+str(p))
        assert ok,(p,error)
    original=earthd.saved_cache_dir
    original_argv=sys.argv
    try:
        # main() eagerly evaluates saved_cache_dir even for --help; avoid user settings.
        earthd.saved_cache_dir=lambda:str(root/'help-only-cache-never-created')
        sys.argv=['earthd','--help']
        with contextlib.redirect_stdout(io.StringIO()) as help_text:
            try: earthd.main()
            except SystemExit as error: assert error.code==0
        assert '--user-dir' in help_text.getvalue() and '--cache-dir' in help_text.getvalue()
        (root/'production-cli-help.txt').write_text(help_text.getvalue(),encoding='utf-8')
    finally:
        earthd.saved_cache_dir=original
        sys.argv=original_argv
    result=dict(archive_files_verified=len(audit['files']),required_components=len(audit['required_files']),
        native_version=fast._ef.VERSION,webp=True,python_files_parsed=len(python_files),
        lua_files_compiled_luajit21=len(lua_files),production_import_and_help=True,
        production_saved_path_discovery_bypassed_for_help=True,engine_tested=False,
        manual_run_directory_created=(root/'run').exists())
    (root/'runtime-audit.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__': main()
