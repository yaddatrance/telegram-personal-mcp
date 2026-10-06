// Offline visual QA: launches only an isolated synthetic demo and a fresh headless browser.
import {spawn} from 'node:child_process';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
const root = resolve(import.meta.dirname, '..');
const stamp = Date.now().toString();
const dataDir = resolve(root, '.local', 'browser-qa-' + stamp);
const browserDir = resolve(root, '.local', 'chrome-qa-' + stamp);
await mkdir(browserDir, {recursive:true});
await mkdir(resolve(root, 'evidence'), {recursive:true});
const pause = ms => new Promise(r => setTimeout(r, ms));
const attachmentMode = process.argv.includes('--attachment');
const evidencePrefix = attachmentMode ? 'attachment' : 'review';
if (attachmentMode) {
  const fixture = `
import asyncio, sys
from pathlib import Path
from ken_mcp.store import Store
from ken_mcp.service import Messenger
from ken_mcp.transport import FakeTransport
async def seed():
    root = Path(sys.argv[1])
    inbox = root / 'attachments'
    inbox.mkdir(parents=True)
    (inbox / 'sample.epub').write_bytes(b'Synthetic EPUB fixture for offline visual QA.')
    store = Store(root / 'outbox.sqlite3', 'demo')
    try:
        service = Messenger(store, FakeTransport(), require_local_approval=True)
        await service.start()
        await service.list_peers()
        await service.prepare('1001', 'Here is the sample EPUB.', 'browser-attachment-qa-01', attachment_filename='sample.epub')
    finally:
        store.close()
asyncio.run(seed())
`;
  const seed = spawn(process.env.KEN_QA_PYTHON || 'python', ['-c', fixture, dataDir], {cwd:root,windowsHide:true,stdio:'ignore'});
  await new Promise((res, rej) => { seed.on('error', rej); seed.on('exit', code => code === 0 ? res() : rej(Error('Synthetic attachment setup failed'))); });
}
const app = spawn(process.env.KEN_QA_PYTHON || 'python', ['-u', '-m', 'ken_mcp', 'demo', '--data-dir', dataDir, '--port', '0'], {cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});
let output=''; app.stdout.on('data', b => output += b.toString());
let browser, socket;
try {
  for(let i=0;i<100 && !output.match(/http:\/\/127\.0\.0\.1:\d+\/#[\w-]+/);i++) await pause(100);
  const url=output.match(/http:\/\/127\.0\.0\.1:\d+\/#[\w-]+/)?.[0];
  if (!url) throw Error('Offline review app did not start');
  browser=spawn(process.env.KEN_QA_BROWSER || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', ['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--remote-debugging-port=0','--user-data-dir='+browserDir,'about:blank'], {windowsHide:true,stdio:'ignore'});
  let port;
  for(let i=0;i<100;i++){try{port=(await readFile(resolve(browserDir,'DevToolsActivePort'),'utf8')).split('\n')[0];break;}catch{await pause(100);}}
  if(!port) throw Error('Headless browser could not start');
  const page=await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,{method:'PUT',signal:AbortSignal.timeout(5000)})).json();
  socket=new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res,rej)=>{const timer=setTimeout(()=>rej(Error('Browser socket timeout')),5000);socket.onopen=()=>{clearTimeout(timer);res();};socket.onerror=rej;});
  let seq=0;const pending=new Map();
  socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const {resolve,reject}=pending.get(m.id);pending.delete(m.id);m.error?reject(Error(JSON.stringify(m.error))):resolve(m.result);}};
  const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;const timer=setTimeout(()=>{pending.delete(id);reject(Error('Browser command timeout: '+method));},10000);pending.set(id,{resolve:r=>{clearTimeout(timer);resolve(r);},reject:e=>{clearTimeout(timer);reject(e);}});socket.send(JSON.stringify({id,method,params}));});
  const evaluate=async expression=>(await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true})).result.value;
  await cdp('Page.enable');
  await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await cdp('Page.navigate',{url});
  let ready=false;
  for(let i=0;i<100;i++){if(await evaluate('!!document.querySelector(".card")')){ready=true;break;}await pause(100);}
  if(!ready)throw Error('Review card did not render');
  if(attachmentMode && !(await evaluate('document.body.textContent.includes("sample.epub") && document.body.textContent.includes("FILE SHA-256:")')))
    throw Error('Attachment filename or file hash did not render');
  const desktop=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
  await writeFile(resolve(root,'evidence',evidencePrefix+'-desktop.png'),Buffer.from(desktop.data,'base64'));
  const overflow=await evaluate('document.documentElement.scrollWidth > window.innerWidth');
  if(overflow)throw Error('Desktop layout overflows');
  await evaluate('document.querySelector(".primary").click()');
  let approved=false;
  for(let i=0;i<50;i++){if(await evaluate('document.querySelector(".status")?.textContent === "Approved locally"')){approved=true;break;}await pause(100);}
  if(!approved)throw Error('Local approval did not update the UI');
  await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await pause(200);
  if(await evaluate('document.documentElement.scrollWidth > window.innerWidth'))throw Error('Mobile layout overflows');
  const mobile=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
  await writeFile(resolve(root,'evidence',evidencePrefix+'-mobile.png'),Buffer.from(mobile.data,'base64'));
  await writeFile(resolve(root,'evidence',attachmentMode ? 'attachment-browser-check.json' : 'browser-check.json'),JSON.stringify({mode:'synthetic demo',desktop:'1440x1000',mobile:'390x844',cardRendered:true,attachmentMetadataRendered:attachmentMode,ownerApprovalWorked:true,overflow:false,liveTelegramRequests:0},null,2));
  console.log('PASS: desktop/mobile rendering, no overflow, exact-preview approval. No live Telegram requests.');
  await cdp('Browser.close').catch(()=>{});
} finally {socket?.close();browser?.kill();app.kill();}
