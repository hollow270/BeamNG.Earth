"""Real companion/socket re-anchoring; acknowledgements are SIMULATED, never engine evidence."""
import asyncio
import json


async def smoke(server,port,transport,scenario,protocol,root):
    import geodesy
    reader,writer=await asyncio.open_connection('127.0.0.1',port)
    queue=asyncio.Queue(maxsize=256); packets=[]; states=[]; pose={}; anchors=[]
    async def send(message):
        writer.write(protocol.encode(message)); await writer.drain()
    async def read():
        while True:
            message=await protocol.read_message(reader)
            if message is None: raise RuntimeError('Companion disconnected during restoration smoke')
            if message['type'] in ('building_activate','building_activate_batch'):
                packets.append(message)
                if len(packets)>32: raise RuntimeError('Too many activation batches')
            if message['type']=='building_status':
                states.append(dict(state=message['state'],restoration=message.get('restoration'),error=message.get('error')))
                if len(states)>256: raise RuntimeError('Restoration did not settle within status quota')
            await queue.put(message)
    async def until(predicate):
        async with asyncio.timeout(60):
            while True:
                message=await queue.get()
                if message['type']=='building_status' and message['state']=='failed':
                    raise RuntimeError(str(message.get('restoration') or message.get('error')))
                if predicate(message): return message
    async def poses():
        while True:
            if pose: await send(dict(pose))
            await asyncio.sleep(.25)
    async def anchor(identity,lat,lon):
        await send(dict(type='prepare_anchor',requestId=identity,lat=lat,lon=lon))
        ready=await until(lambda m:m['type']=='anchor_ready' and m['requestId']==identity)
        await send(dict(type='commit_anchor',anchorId=ready['anchorId']))
        viewpoint=scenario.manifest()['viewpoint']
        x,y,_=geodesy.EnuFrame(lat,lon).geodetic_to_enu(viewpoint['lat'],viewpoint['lon'],0)
        pose.update(type='pose',anchorId=ready['anchorId'],x=float(x),y=float(y),z=2,vx=0,vy=0,
                    lat=viewpoint['lat'],lon=viewpoint['lon'],speed=0)
        anchors.append(ready['anchorId'])
    async def activated():
        status=await until(lambda m:m['type']=='building_status' and m['state']=='activation_scheduled')
        batch=next(m for m in reversed(packets) if m['transaction']==status['transaction'])
        await send(dict(type='building_ack',**{k:batch[k] for k in ('sessionId','epoch','anchorId','seq','transaction')},
                        status='activated',detail='TEST ONLY simulated acknowledgement; not an engine result'))
        await until(lambda m:m['type']=='building_status' and m['state']=='activated' and m['transaction']==batch['transaction'])
    reading=asyncio.create_task(read()); posing=asyncio.create_task(poses())
    try:
        await send(dict(type='hello',protocol=4,format=4,modVersion='stage6d-restoration-smoke',buildingActivation=1,
                        buildingBatches=1,buildingControl=1,buildingMultiplayer=False))
        hello=await until(lambda m:m['type']=='hello')
        await anchor(1,**scenario.manifest()['viewpoint'])
        await until(lambda m:m['type']=='roads_tile')
        conn=next(iter(server.connections))
        async with asyncio.timeout(60):
            while not conn.roads.car.issubset(conn.roads.sent|conn.roads.empty) or conn.roads.futs:
                await asyncio.sleep(.1)
        async def command(op,identity,**fields):
            await send(dict(type='building_control',op=op,id=identity,sessionId=hello['sessionId'],epoch=hello['buildingEpoch'],
                            authorization=hello['buildingControlToken'],**fields))
        await command('preview',1,bounds=list(scenario.bounds()))
        preview=await until(lambda m:m['type']=='building_status' and m['state']=='awaiting_acceptance')
        await asyncio.sleep(.3)
        await command('accept',2,preview_token=preview['preview']['token'],confirm=True)
        await activated()
        # Same location, new anchor; then a different frame viewing the accepted area.
        await anchor(2,**scenario.manifest()['viewpoint']); await activated()
        await anchor(3,39.29,-76.58); await activated()
        transactions={m['transaction'] for m in packets}
        if len(transactions)!=3 or len(set(anchors))!=3 or transport.calls!=1:
            raise RuntimeError('Duplicate transactions, missing re-anchor or unexpected refetch')
        result=dict(synthetic=True,real_server_and_socket=True,engine_tested=False,acknowledgements='SIMULATED',
                    anchors=anchors,transactions=len(transactions),fixture_requests=transport.calls,
                    batch_targets=[len(m['targets']) for m in packets],states=states)
        (root/'restoration-smoke-results.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result),flush=True)
    finally:
        reading.cancel(); posing.cancel(); await asyncio.gather(reading,posing,return_exceptions=True)
        writer.close(); await writer.wait_closed()
