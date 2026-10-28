import test from 'node:test';
import assert from 'node:assert/strict';
import {AlbumSaver} from '../static/autosave.js';
import {LayoutTarget} from '../static/async_target.js';
const project = () => ({id:'album-one', revision:1, title:'Initial', photo_ids:[], spreads:[{id:'spread-a',slots:[]},{id:'spread-b',slots:[]}], design_format:'legacy'});
const memory = () => {
  const values = new Map();
  return {getItem:key=>values.get(key), setItem:(key,value)=>values.set(key,value), removeItem:key=>values.delete(key), get length(){return values.size;}, key:index=>[...values.keys()][index]};
};
const saver = options => new AlbumSaver({storage:memory(), schedule:()=>0, cancel:()=>{}, ...options});
const deferred = () => { let resolve, reject; const promise = new Promise((yes,no)=>{resolve=yes;reject=no;}); return {promise,resolve,reject}; };

test('connection failure retains durable draft and retries the exact operation', async () => {
  const storage = memory(), sent = [];
  let fail = true;
  const controller = saver({storage, send:async(id,body)=>{
    sent.push(structuredClone(body)); if(fail) throw Error('Connection lost');
    return {...body,revision:2};
  }});
  const p = controller.open(project()); p.title='Unsaved title'; controller.edit(p);
  await assert.rejects(controller.flush(p.id), /Connection lost/);
  assert.equal(controller.status(p.id),'error'); assert.ok(controller.dirty());
  fail=false; await controller.flush(p.id);
  assert.deepEqual(sent[0],sent[1]); assert.equal(controller.status(p.id),'saved'); assert.ok(!controller.dirty());
});

test('refresh recovers an ambiguous committed save and then sends the newest edit', async () => {
  const storage = memory(), gate=deferred(), sent=[];
  const first=saver({storage,send:async(id,body)=>{sent.push(structuredClone(body)); return gate.promise;}});
  const p=first.open(project()); p.title='First edit'; first.edit(p);
  p.title='Newest edit'; first.edit(p);
  const second=saver({storage,send:async(id,body)=>{sent.push(structuredClone(body)); return {...body,revision:body.revision+1};}});
  const recovered=second.open({...project(),revision:2,title:'First edit'});
  assert.equal(recovered.title,'Newest edit');
  await second.flush(p.id);
  assert.deepEqual(sent[0],sent[1]);
  assert.equal(sent[2].title,'Newest edit'); assert.equal(sent[2].revision,2);
  assert.notEqual(sent[2]._operation_id,sent[1]._operation_id);
  gate.resolve({...sent[0],revision:2}); await first.flush(p.id);
});

test('conflict is visible, preserves draft, and never rebases to the newer server revision', async () => {
  const storage=memory(); let sent=0;
  const first=saver({storage,send:async()=>{sent++; throw Object.assign(Error('Other editor'),{status:409});}});
  const p=first.open(project()); p.title='My work'; first.edit(p);
  await assert.rejects(first.flush(p.id),/Other editor/);
  const second=saver({storage,send:async()=>{sent++; throw Object.assign(Error('Other editor'),{status:409});}});
  assert.equal(second.open({...project(),revision:3}).title,'My work');
  await assert.rejects(second.flush(p.id),/Other editor/);
  assert.equal(second.status(p.id),'conflict'); assert.equal(second.draft(p.id).title,'My work');
  assert.equal(sent,2);
});

test('albums have independent revisions and outboxes when switching during a save', async () => {
  const gate=deferred(), sent=[];
  const controller=saver({send:async(id,body)=>{sent.push({id,...body}); return id==='album-one'?gate.promise:{...body,revision:8};}});
  const a=controller.open(project()); a.title='A'; controller.edit(a);
  const b=controller.open({...project(),id:'album-two',revision:7}); b.title='B'; controller.edit(b);
  await controller.flush(b.id); assert.equal(sent[1].revision,7);
  gate.resolve({...a,revision:2}); await controller.flush(a.id);
  assert.equal(sent[0].revision,1); assert.ok(!controller.dirty());
});

test('storage failure stops network writes and keeps an in-memory downloadable draft', async () => {
  let sent=0;
  const controller=saver({storage:{getItem:()=>null,setItem:()=>{throw Error('Quota');}}, send:async()=>{sent++;}});
  const p=controller.open(project()); p.title='Keep this'; controller.edit(p);
  assert.equal(sent,0); assert.equal(controller.status(p.id),'error');
  assert.equal(controller.draft(p.id).title,'Keep this'); assert.ok(controller.dirty());
});

test('a layout answer cannot move across spreads, projects, edits or request order', () => {
  const guard=new LayoutTarget(), p=project();
  const target=guard.capture(p,0);
  assert.ok(guard.matches(target,p,0));
  assert.ok(!guard.matches(target,p,1));
  assert.ok(!guard.matches(target,{...p,id:'album-two'},0));
  p.spreads[0].slots.push({photo_id:'new'}); assert.ok(!guard.matches(target,p,0));
  const old=guard.capture(p,0), fresh=guard.capture(p,0);
  assert.ok(!guard.matches(old,p,0)); assert.ok(guard.matches(fresh,p,0));
  guard.invalidate(); assert.ok(!guard.matches(fresh,p,0));
});


test('two tabs keep separate durable drafts and archived drafts can be recovered', async () => {
  const storage=memory();
  const send=async()=>{throw Object.assign(Error('Conflict'),{status:409});};
  const a=saver({storage,clientId:'tab-a',send}), b=saver({storage,clientId:'tab-b',send});
  const first=a.open(project()), second=b.open(project());
  first.title='Tab A work'; second.title='Tab B work'; a.edit(first); b.edit(second);
  await assert.rejects(a.flush(first.id)); await assert.rejects(b.flush(second.id));
  assert.equal(a.draft(first.id).title,'Tab A work'); assert.equal(b.draft(second.id).title,'Tab B work');
  a.discard(first.id);
  a.open({...project(),revision:3});
  const archived=a.recoverable(first.id).find(item=>item.title==='Tab A work');
  assert.ok(archived);
  assert.equal(a.recover(first.id,archived.key,{...project(),revision:3}).title,'Tab A work');
  assert.equal(a.status(first.id),'conflict');
});


test('browser timer functions are called without the saver as their receiver', async () => {
  const originalSet=globalThis.setTimeout, originalClear=globalThis.clearTimeout;
  try {
    globalThis.setTimeout=function(){assert.equal(this,undefined);return 1;};
    globalThis.clearTimeout=function(){assert.equal(this,undefined);};
    const controller=new AlbumSaver({storage:memory(),send:async()=>{throw Error('Offline');}});
    const p=controller.open(project()); p.title='Browser draft'; controller.edit(p);
    await assert.rejects(controller.flush(p.id),/Offline/);
    assert.equal(controller.status(p.id),'error');
  } finally {globalThis.setTimeout=originalSet;globalThis.clearTimeout=originalClear;}
});


test('a rejected invalid draft can be corrected without replaying the invalid write forever', async () => {
  const sent=[];
  const controller=saver({send:async(id,body)=>{
    sent.push(structuredClone(body));
    if(body.title==='Invalid') throw Object.assign(Error('Invalid draft'),{status:400});
    return {...body,revision:2};
  }});
  const p=controller.open(project()); p.title='Invalid'; controller.edit(p);
  await assert.rejects(controller.flush(p.id),/Invalid draft/);
  p.title='Corrected'; controller.edit(p); await controller.flush(p.id);
  assert.equal(sent[1].title,'Corrected'); assert.equal(sent[1].revision,1);
  assert.notEqual(sent[0]._operation_id,sent[1]._operation_id);
  assert.equal(controller.status(p.id),'saved');
});
