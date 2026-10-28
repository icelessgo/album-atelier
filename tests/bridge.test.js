import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import * as crypto from 'node:crypto';
import fs from 'node:fs';
const source=fs.readFileSync(new URL('../picpeak_bridge.cjs',import.meta.url),'utf8');
async function run(mode,{missing=[],eventType='wedding'}={}) {
  let initialized=false, destroyed=false, exit=0;
  const stdout=[],stderr=[];
  const photos=[1,2].map(id=>({id,event_id:7,processing_status:'complete',media_type:'photo',filename:`photo-${id}.jpg`}));
  function db(table) {
    let rows=table==='events'?[{id:7,event_type:eventType,is_archived:false,is_active:true,slug:'synthetic-gallery'}]:table==='photos'?photos:[];
    const query={where(value,expected){rows=typeof value==='object' ? rows.filter(row=>Object.entries(value).every(([key,v])=>row[key]===v)) : rows.filter(row=>row[value]===expected);return query;},
      whereIn(key,values){rows=rows.filter(row=>values.includes(row[key]));return query;},
      whereNot(){return query;}, select(){return query;},count(){return query;},groupBy(){return query;},orderBy(){return query;},first:async()=>rows[0],
      then(resolve,reject){return Promise.resolve(rows).then(resolve,reject);}};
    return query;
  }
  const finished=new Promise(resolve=>{
    db.destroy=async()=>{destroyed=true;resolve();};
    const storage={kind:()=> 's3',stat:async key=>{if(!initialized)throw Error('generation index not initialized');return {size:100};},
      get:async key=>{assert.ok(initialized);if(missing.includes(key)) throw Error('Object unavailable');return [Buffer.from('restored-generation-pixels')];}};
    const sharp=()=>({rotate(){return this;},resize(){return this;},jpeg(){return this;},toBuffer:async()=>Buffer.from('preview')});
    const context={Buffer, console, setTimeout, clearTimeout, require:name=>{
      if(name.endsWith('/database/db')) return {db};
      if(name.endsWith('/services/storage'))return {initStorage:async()=>{initialized=true;},getStorage:()=>storage};
      if(name.endsWith('/services/photoResolver'))return {resolvePhotoStorageKey:(event,photo)=>`restored/${photo.id}`};
      if(name.endsWith('/utils/galleryLifecycle'))return {isGalleryAvailable:event=>Boolean(event?.is_active && !event.is_archived)};
      if(name.endsWith('/utils/revealMode'))return {isGalleryHidden:()=>false};
      if(name==='sharp')return sharp;
      if(name==='node:crypto')return crypto;
      throw Error('Unexpected dependency');
    },process:{argv:['node','-',mode,'7','1'],stdout:{write:(value,done)=>{stdout.push(Buffer.from(value));done();}},
      stderr:{write:value=>stderr.push(value)},exit:code=>{exit=code;resolve();}}};
    vm.runInNewContext(source,context);
  });
  await finished;
  return {initialized,destroyed,exit,output:Buffer.concat(stdout).toString(),error:stderr.join('')};
}
test('preview bridge initializes generation-aware storage before reading',async()=>{
  const result=await run('previews'); assert.equal(result.exit,0); assert.ok(result.initialized&&result.destroyed);
  const rows=result.output.trim().split('\n').map(JSON.parse);
  assert.equal(rows.filter(row=>row.photo).length,2);
});
test('original bridge initializes storage and returns the resolved generation object',async()=>{
  const result=await run('original'); assert.equal(result.exit,0); assert.equal(result.output,'restored-generation-pixels');
});
test('zero readable previews fail instead of reporting an empty successful import',async()=>{
  const result=await run('previews',{missing:['restored/1','restored/2']});
  assert.equal(result.exit,1); assert.equal(JSON.parse(result.output).summary.imported,0); assert.match(result.error,/No readable photos/);
});
test('partial imports retain readable photos and report the skipped count',async()=>{
  const result=await run('previews',{missing:['restored/2']}); assert.equal(result.exit,0);
  assert.match(result.error,/1 imported; 1 unavailable/);
});

test('source check returns a private fingerprint without returning storage keys',async()=>{
  const result=await run('sources'); assert.equal(result.exit,0);
  const value=JSON.parse(result.output); assert.equal(value.available,true);
  assert.match(value.photos[0].version,/^[a-f0-9]{64}$/); assert.ok(!result.output.includes('restored/'));
});

test('Studio lists wedding galleries only',async()=>{
  const wedding=await run('galleries');
  assert.equal(JSON.parse(wedding.output)[0].event_type,'wedding');
  const family=await run('galleries',{eventType:'family'});
  assert.deepEqual(JSON.parse(family.output),[]);
});

test('non-wedding imports, originals and source checks cannot bypass the gallery picker',async()=>{
  for (const eventType of ['family','other',undefined]) {
    // Explicit null covers missing/unknown metadata without using the default.
    const options={eventType:eventType??null};
    for(const mode of ['previews','original']) {
      const result=await run(mode,options);
      assert.equal(result.exit,1);
      assert.match(result.error,/wedding galleries only/);
      assert.equal(result.initialized,false);
    }
    const result=await run('sources',options);
    assert.equal(JSON.parse(result.output).available,false);
    assert.equal(result.initialized,false);
  }
});
