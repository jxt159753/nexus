// Pure event-loop regression: no browser, network, package or DOM dependencies.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");

const elements = new Map();
const nodeId = "23c97752-881b-4d05-a14a-75ba6adf0814";
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value:id === "node" ? nodeId : "", hidden:false,
    replaceChildren() {}, reset() {}, classList:{toggle() {}}, setAttribute() {},
  });
  return elements.get(id);
}
class Socket {
  static OPEN = 1;
  static instances = [];
  constructor() {this.readyState=1; this.sent=[]; Socket.instances.push(this);}
  send(raw) {this.sent.push(JSON.parse(raw));}
  close() {this.closed=true; this.readyState=3;}
}
const context = vm.createContext({
  document:{getElementById:element,querySelectorAll:() => []},
  window:{addEventListener() {}}, location:{href:"http://127.0.0.1:8000/",protocol:"http:"},
  WebSocket:Socket, URL, crypto, AbortSignal,
  setInterval() {}, setTimeout() {return 1;}, clearTimeout() {},
  // The initial session fetch stays pending; task-detail processing fails.
  fetch:url => url === "/api/v1/session" ? new Promise(() => {}) : Promise.reject(new Error("injected detail failure")),
});
vm.runInContext(fs.readFileSync(process.argv[2] || path.join(__dirname,"../src/nexus/web/app.js"),"utf8"),context);
const run = source => vm.runInContext(source,context);
const settle = async () => {for (let i=0;i<5;i++) await new Promise(setImmediate);};
function event(socket,type,payload) {socket.onmessage({data:JSON.stringify({type,payload})});}
(async () => {
  run("state.logged=true; connect()");
  const first = Socket.instances[0];
  event(first,"client.synced",{event_id:"0",full_sync_required:false});
  await settle();
  run("submit('get_system_info')");
  const submission = first.sent.find(message => message.type === "task.submit");
  assert.ok(submission);
  event(first,"task.updated",{event_id:"1",task_id:"a"});
  event(first,"control.updated",{event_id:"2",paused:true,revision:20});
  await settle();
  assert.equal(run("state.cursor"),null,"a later queued event advanced past a failed event");
  assert.equal(run("state.controlRevision"),-1,"queued work continued after failure");
  assert.equal(first.closed,true);
  run("connect()");
  const second = Socket.instances[1];
  event(second,"client.synced",{event_id:"0",full_sync_required:false});
  await settle();
  const retry = second.sent.find(message => message.type === "task.submit");
  assert.deepEqual(retry.payload,submission.payload,"reconnect changed the task idempotency payload");
  assert.notEqual(retry.message_id,submission.message_id);
  console.log("Passed: failed-event cursor barrier and stable request retry");
})().catch(error => {console.error(error); process.exitCode=1;});
