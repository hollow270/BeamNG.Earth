"""Checks for the development tools; creates only repository-local temporary data."""
import json
import asyncio
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

REPO=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(REPO/'server'),str(Path(__file__).resolve().parent)]
import scenario
import serve
import buildingoverlay as bo
import osmbuildings as osm
import restoration_smoke


class PreparationTest(unittest.TestCase):
    def smoke_failure(self,*,osm_enabled,activated_enabled=True,accepted='a'*64,activated='a'*64):
        async def scenario_test():
            queue=asyncio.Queue()
            server=SimpleNamespace(connections=[SimpleNamespace(roads=SimpleNamespace(car={(0,0)},sent={(0,0)},empty=set(),futs={}))])
            class Writer:
                def write(self,message):
                    kind=message['type']
                    if kind=='hello': queue.put_nowait(dict(type='hello',sessionId='test',buildingEpoch='e'*32,buildingControlToken='b'*64))
                    elif kind=='prepare_anchor': queue.put_nowait(dict(type='anchor_ready',requestId=message['requestId'],anchorId=1))
                    elif kind=='commit_anchor': queue.put_nowait(dict(type='roads_tile'))
                    elif kind=='building_control':
                        if message['op']=='preview': queue.put_nowait(dict(type='building_status',state='awaiting_acceptance',preview=dict(token='c'*64)))
                        else:
                            queue.put_nowait(dict(type='building_activate',sessionId='test',epoch='e'*32,anchorId=1,seq=1,
                                                 transaction='d'*32,osm=osm_enabled,targets=[]))
                            queue.put_nowait(dict(type='building_status',state='activation_scheduled',transaction='d'*32))
                    elif kind=='building_ack':
                        queue.put_nowait(dict(type='building_status',state='activated',transaction='d'*32,
                            activated_enabled=activated_enabled,accepted_revision=accepted,activated_revision=activated))
                async def drain(self): pass
                def close(self): pass
                async def wait_closed(self): pass
            async def read(reader): return await queue.get()
            protocol=SimpleNamespace(encode=lambda m:m,read_message=read)
            with mock.patch.object(asyncio,'open_connection',new=mock.AsyncMock(return_value=(None,Writer()))):
                await restoration_smoke.smoke(server,12345,SimpleNamespace(calls=1),scenario,protocol,REPO)
        return scenario_test()

    def test_restoration_smoke_rejects_baseline_fallback_without_writing_results(self):
        with self.assertRaisesRegex(RuntimeError,'baseline fallback'):
            asyncio.run(self.smoke_failure(osm_enabled=False))

    def test_restoration_smoke_requires_enabled_matching_accepted_revision(self):
        for flags in (dict(activated_enabled=False),dict(activated='f'*64),dict(accepted=None,activated=None)):
            with self.subTest(flags=flags),self.assertRaisesRegex(RuntimeError,'enabled accepted revision'):
                asyncio.run(self.smoke_failure(osm_enabled=True,**flags))

    def test_fixture_uses_the_real_parser_and_conservative_merge(self):
        source=osm.parse(scenario.PAYLOAD,scenario.bounds(),now='2026-10-30T12:01:00Z')
        baseline=scenario.originals()
        before=bo.MergeResult(baseline,(),()).digest()
        merged=bo.merge(baseline,source.buildings)
        self.assertEqual(merged.report()['counts'],dict(addition=1,replacement=1,unchanged=2,conflict=0))
        self.assertEqual(before,bo.MergeResult(baseline,(),()).digest())
        self.assertEqual(len(merged.buildings),4)

    def test_fixture_retains_ids_and_geometry_and_does_not_need_an_http_service(self):
        rows=osm.parse(scenario.PAYLOAD,scenario.bounds(),now='2026-10-30T12:01:00Z').buildings
        self.assertEqual({r.feature_id for r in rows},{'osm:way:9001','osm:way:9002','osm:way:9003'})
        events=[]; transport=scenario.FixtureTransport(lambda *a,**k:events.append((a,k)))
        self.assertEqual(transport.post(scenario.ENDPOINT,b'',{},max_bytes=8*1024*1024)[2],scenario.PAYLOAD)
        with self.assertRaises(RuntimeError): transport.post('https://not-permitted.invalid',b'',{},max_bytes=999999)
        self.assertEqual(transport.calls,1)

    def test_workspace_requires_marker_and_child_paths_cannot_escape(self):
        tmp_root=REPO/'.venv/stage6a'; tmp_root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
            root=Path(tmp)
            with self.assertRaises(RuntimeError): serve.workspace(root)
            (root/'STAGE6A_WORKSPACE.json').write_text(json.dumps({'schema':'stage6a-workspace-v1'}))
            self.assertEqual(serve.workspace(root),root)
            self.assertEqual(serve.safe_child(root,root/'run'),root/'run')
            with self.assertRaises(RuntimeError): serve.safe_child(root,root.parent/'other-kit')

    def test_external_dns_and_sockets_are_blocked_before_any_network_access(self):
        with mock.patch.object(socket.socket,'connect',socket.socket.connect), \
             mock.patch.object(socket.socket,'connect_ex',socket.socket.connect_ex), \
             mock.patch.object(socket,'getaddrinfo',socket.getaddrinfo):
            serve.no_external_network()
            with self.assertRaises(RuntimeError): socket.getaddrinfo('example.invalid',443)
            with socket.socket() as connection:
                with self.assertRaises(RuntimeError): connection.connect(('192.0.2.1',443))

    def test_linked_workspace_is_rejected_before_reading_its_marker(self):
        tmp_root=REPO/'.venv/stage6a'; tmp_root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
            root=Path(tmp)
            (root/'STAGE6A_WORKSPACE.json').write_text('Not JSON: must never be read')
            with mock.patch.object(Path,'is_junction',lambda p:p==root):
                with self.assertRaisesRegex(RuntimeError,'links or junctions'): serve.workspace(root)

    def test_fixture_bounds_are_small_and_payload_is_deterministic(self):
        w,s,e,n=scenario.bounds()
        self.assertLess((n-s)*111320,500)
        self.assertLess((e-w)*111320,500)
        self.assertEqual(scenario.manifest(),scenario.manifest())
        self.assertEqual(json.loads(scenario.PAYLOAD)['elements'][-1]['tags']['total'],'15')


if __name__=='__main__': unittest.main()
