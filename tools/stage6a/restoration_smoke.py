"""Real companion/socket re-anchoring; acknowledgements are SIMULATED, never engine evidence."""
import asyncio
import json


async def smoke(server,port,transport,scenario,protocol,root,*,cycles=3):
    import geodesy
    if type(cycles) is not int or not 3<=cycles<=16: raise ValueError('invalid bounded smoke cycle count')
    reader,writer=await asyncio.open_connection('127.0.0.1',port)
    queue=asyncio.Queue(maxsize=256); packets=[]; states=[]; pose={}; anchors=[]; confirmed=set()
    scene_slots=[]; inventories=[]; capacities=[]; pauses=0
    async def send(message):
        writer.write(protocol.encode(message)); await writer.drain()
    async def read():
        while True:
            message=await protocol.read_message(reader)
            if message is None: raise RuntimeError('Companion disconnected during restoration smoke')
            if message['type'] in ('building_activate','building_activate_batch'):
                packets.append(message)
                if len(packets)>cycles*4: raise RuntimeError('Too many activation batches')
            if message['type']=='building_status':
                states.append(dict(state=message['state'],restoration=message.get('restoration'),error=message.get('error')))
                capacity=(message.get('resources') or {}).get('capacity')
                if capacity: capacities.append(dict(capacity))
                if len(states)>cycles*64: raise RuntimeError('Restoration did not settle within status quota')
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
            if pose:
                message=dict(pose); pose.pop('slotsGone',None)
                await send(message)
            await asyncio.sleep(.25)
    async def anchor(identity,lat,lon):
        await send(dict(type='prepare_anchor',requestId=identity,lat=lat,lon=lon))
        ready=await until(lambda m:m['type']=='anchor_ready' and m['requestId']==identity)
        await send(dict(type='commit_anchor',anchorId=ready['anchorId']))
        viewpoint=scenario.manifest()['viewpoint']
        x,y,_=geodesy.EnuFrame(lat,lon).geodetic_to_enu(viewpoint['lat'],viewpoint['lon'],0)
        pose.update(type='pose',anchorId=ready['anchorId'],x=float(x),y=float(y),z=2,vx=0,vy=0,
                    lat=viewpoint['lat'],lon=viewpoint['lon'],speed=0)
        # TEST ONLY: this protocol client has no engine. Model exact old-scene
        # release evidence through the normal serial-fenced slotsGone path.
        if scene_slots:
            pose['slotsGone']=list(scene_slots); scene_slots.clear()
        anchors.append(ready['anchorId'])
    async def activated():
        nonlocal pauses
        status=await until(lambda m:m['type']=='building_status' and m['state']=='activation_scheduled' and m['transaction'] not in confirmed)
        batch=next(m for m in reversed(packets) if m['transaction']==status['transaction'])
        if batch.get('osm') is not True:
            raise RuntimeError('Restoration smoke received baseline fallback instead of accepted overlays')
        fields={k:batch[k] for k in ('sessionId','epoch','anchorId','seq','transaction')}
        if len(anchors)%4==0:
            pauses+=1
            await send(dict(type='building_ack',**fields,status='deferred',detail='TEST ONLY simulated paused controller'))
            await until(lambda m:m['type']=='building_status' and m['state']=='activation_deferred')
            await asyncio.sleep(.6)  # pending transaction must not prepare another batch
        await send(dict(type='building_ack',**fields,
                        status='activated',detail='TEST ONLY simulated acknowledgement; not an engine result'))
        active=await until(lambda m:m['type']=='building_status' and m['state']=='activated' and m['transaction']==batch['transaction'])
        if active.get('activated_enabled') is not True or not active.get('accepted_revision') or active.get('accepted_revision')!=active.get('activated_revision'):
            raise RuntimeError('Restoration smoke did not confirm the enabled accepted revision')
        confirmed.add(batch['transaction'])
        for message in packets:
            if message['transaction']==batch['transaction']:
                scene_slots.extend([int(path.rsplit('slot_',1)[-1].split('.')[0]),serial]
                    for target in message['targets'] for path,serial in target['slots'].values())
        coordinator=server._building_refresh
        def inventory():
            with coordinator.io._locked(): return coordinator._usage()[0]
        inventories.append(await asyncio.to_thread(inventory))
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
        # Alternate same-location resets and a different frame viewing this area.
        for identity in range(2,cycles+1):
            if identity%3==0: await anchor(identity,39.29,-76.58)
            else: await anchor(identity,**scenario.manifest()['viewpoint'])
            await activated()
        transactions={m['transaction'] for m in packets}
        if len(transactions)!=cycles or len(set(anchors))!=cycles or transport.calls!=1:
            raise RuntimeError('Duplicate transactions, missing re-anchor or unexpected refetch')
        if max(inventories)>server._building_refresh.limits.artifacts:
            raise RuntimeError('Generation count exceeded the unchanged quota')
        if cycles>=8 and not any(c['passes'] for c in capacities):
            raise RuntimeError('Long replay did not exercise generation reclamation')
        result=dict(synthetic=True,real_server_and_socket=True,engine_tested=False,acknowledgements='SIMULATED',
                    anchors=anchors,transactions=len(transactions),fixture_requests=transport.calls,
                    batch_targets=[len(m['targets']) for m in packets],states=states,
                    generation_counts=inventories,capacity_snapshots=capacities,simulated_pauses=pauses,
                    slot_release_evidence='SIMULATED serial-fenced slotsGone')
        (root/'restoration-smoke-results.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result),flush=True)
    finally:
        reading.cancel(); posing.cancel(); await asyncio.gather(reading,posing,return_exceptions=True)
        writer.close(); await writer.wait_closed()
