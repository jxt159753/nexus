
const id = '[0-9a-f-]{36}';
const fileURL = new RegExp(`^/api/v1/(plugins/deliverables/${id}|chat/conversations/${id}/attachments/${id}/content)$`,'i');
const el=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
const button=(label,action)=>{const node=el('button',label);node.type='button';node.style.cssText='padding:7px 12px;margin:4px;border-radius:8px;font:inherit;background:#202020;color:inherit;border:1px solid #404040';node.onclick=action;return node;};
const decode=text=>Uint8Array.from(atob(text),c=>c.charCodeAt(0));

async function bytes(file,uuid) {
  if(!fileURL.test(file.download_url))throw Error('文件地址不可用');
  const native=window.__NEXUS_NATIVE__;let value;
  if(native) {
    if(file.source_kind==='deliverable') {
      const result=await native.readDeliverable(file.download_url);
      if(result.status!==200)throw Error('文件读取失败');
      value=decode(result.body_base64);
    } else {
      const result=await native.attachmentRequest(uuid(),'GET',file.download_url+'?encoding=base64',null,{});
      if(result.status!==200)throw Error('文件读取失败');
      value=decode(JSON.parse(result.body).data_base64);
    }
  } else {
    const response=await fetch(file.download_url,{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(45000)});
    if(!response.ok)throw Error('文件读取失败');
    const reader=response.body.getReader(),chunks=[];let length=0;
    try { while(true) {const {done,value:chunk}=await reader.read();if(done)break;length+=chunk.length;if(length>16*1024*1024)throw Error('文件超过上限');chunks.push(chunk);} }
    finally {await reader.cancel();}
    value=new Uint8Array(length);let offset=0;for(const chunk of chunks){value.set(chunk,offset);offset+=chunk.length;}
  }
  const sha=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',value)),b=>b.toString(16).padStart(2,'0')).join('');
  if(value.length!==file.size_bytes || sha!==file.sha256)throw Error('文件完整性校验失败');
  return value;
}

export function renderChatFiles(files,api,uuid) {
  const list=el('section');list.className='chat-file-cards';list.setAttribute('aria-label','会话文件');
  for(const file of (files || []).slice(0,8)) {
    const card=el('article');card.style.cssText='padding:14px;margin:10px 0;border:1px solid #5d657377;border-radius:12px;overflow-wrap:anywhere';
    const note=el('p',`${file.size_bytes} 字节 · ${file.status || '可打开'}`);note.style.fontSize='12px';note.setAttribute('role','status');
    const run=action=>async event=>{const control=event.currentTarget;control.disabled=true;try{await action();}catch(error){note.textContent=error.message || '暂时无法读取文件';}finally{control.disabled=false;}};
    card.append(el('strong',file.filename),note);
    card.append(button('打开预览',run(async()=>{
      const result=await api(file.preview_url),preview=result.preview;
      const dialog=el('dialog');dialog.style.cssText='box-sizing:border-box;width:min(760px,94vw);max-height:85vh;overflow:auto;padding:22px;border-radius:14px;background:#141414;color:#f0f4f8';
      dialog.append(el('h3',file.filename));
      if(preview.kind==='text') {
        const pre=el('pre',preview.text+(preview.truncated?'\n…（预览已截断，下载原件可查看全部内容）':''));pre.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 monospace';dialog.append(pre);
      } else if(preview.kind==='image' && /^image\/(png|jpeg|gif|webp)$/.test(preview.snapshot?.image?.media_type)) {
        const img=el('img');img.alt=file.filename;img.style.maxWidth='100%';img.src=`data:${preview.snapshot.image.media_type};base64,${preview.snapshot.image.data_base64}`;dialog.append(img);
      } else dialog.append(el('p','此格式暂不支持内嵌预览。请下载原件后使用相应应用打开。'));
      dialog.append(button('关闭',()=>dialog.close()));dialog.onclose=()=>dialog.remove();document.body.append(dialog);dialog.showModal();
    })));
    card.append(button('下载原件',run(async()=>{
      const data=await bytes(file,uuid),url=URL.createObjectURL(new Blob([data],{type:'application/octet-stream'}));
      const link=el('a');link.href=url;link.download=file.filename.replace(/[\\/:*?"<>|\u0000-\u001f]/g,'_');document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
      note.textContent='已校验原件并开始下载';
    })));
    list.append(card);
  }
  return list;
}
