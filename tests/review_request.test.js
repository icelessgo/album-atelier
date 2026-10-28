import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../static/review-app.js',import.meta.url),'utf8');
function requestWith(response) {
  const context=vm.createContext({base:'/review/synthetic',fetch:async()=>response});
  vm.runInContext(source.slice(source.indexOf('async function request('),source.indexOf('\nfunction login('))+'\nthis.request=request;',context);
  return context.request;
}
test('an HTML proxy 413 gives a useful message without changing the client draft',async()=>{
  const body={spreads:[{id:'retained-client-spread'}]};
  const request=requestWith({ok:false,status:413,json:async()=>{throw new SyntaxError('Unexpected token <');}});
  await assert.rejects(request('/layout','PUT',body),/too large to save.*changes are still here/);
  assert.equal(body.spreads[0].id,'retained-client-spread');
});
test('JSON errors keep the service message and successful responses are returned',async()=>{
  await assert.rejects(requestWith({ok:false,status:400,json:async()=>({error:'Choose a cover photo'})})('/layout','PUT',{}),/Choose a cover photo/);
  assert.deepEqual(await requestWith({ok:true,json:async()=>({saved:true})})('/api'),{saved:true});
});
