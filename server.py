import os,uuid,time,threading,subprocess,shutil,secrets,hashlib,hmac,re
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI,UploadFile,File,Form,Header,HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
DATA=Path('/tmp/clipforge');DATA.mkdir(exist_ok=True)
KEY=os.environ.get('CLIPFORGE_API_KEY','')
ORIGINS=os.environ.get('CORS_ORIGINS','https://clipforge-ai-ruddy-one.vercel.app,https://clipforge-ai-hs694131-maker.vercel.app').split(',')
app=FastAPI(title='ClipForge API')
app.add_middleware(CORSMiddleware,allow_origins=ORIGINS,allow_methods=['GET','POST'],allow_headers=['X-ClipForge-Key','Content-Type'])
jobs={};lock=threading.Lock();slot=threading.BoundedSemaphore(1)
def auth(key):
 if not KEY: raise HTTPException(503,'Backend access key not configured')
 if not key or not secrets.compare_digest(key,KEY):raise HTTPException(401,'Invalid access key')
def run(args,timeout=300):
 p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
 if p.returncode:raise RuntimeError((p.stderr or p.stdout)[-650:])
 return p.stdout
def youtube(url):
 try:
  u=urlsplit(url)
  return u.scheme=='https' and u.hostname in ('youtube.com','www.youtube.com','m.youtube.com','youtu.be') and not u.username and not u.password and not u.port
 except: return False
def token(jid,expires):
 return str(expires)+'.'+hmac.new(KEY.encode(),f'{jid}.{expires}'.encode(),hashlib.sha256).hexdigest()
def work(jid,src,url,start,duration,captions,custom,language):
 folder=DATA/jid
 try:
  jobs[jid].update(status='processing',message='Preparing video')
  if url:
   if not youtube(url):raise ValueError('Only HTTPS YouTube URLs are supported')
   jobs[jid]['message']='Importing authorized YouTube excerpt'
   run(['yt-dlp','--no-playlist','--no-progress','--no-warnings','--download-sections',f'*{start}-{start+duration+3}','-f','best[height<=720]/best','--merge-output-format','mp4','-o',str(folder/'input.%(ext)s'),url],timeout=240)
   candidates=[p for p in folder.glob('input.*') if p.suffix in ('.mp4','.mkv','.webm')]
   if not candidates:raise RuntimeError('Video import failed or was blocked by source')
   src=candidates[0];offset=0
  else:offset=start
  vf='scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920'
  if captions=='custom' and custom.strip():
   safe=re.sub(r"[^\w\s.,!?-]","",custom)[:160]
   if safe: vf+=',drawtext=text='+safe.replace(':','\\:').replace(',','\\,')+':fontcolor=white:fontsize=45:borderw=3:x=(w-text_w)/2:y=h*0.78'
  if captions=='auto':
   jobs[jid]['message']='Transcribing with Whisper'
   from faster_whisper import WhisperModel
   model=WhisperModel(os.environ.get('WHISPER_MODEL','tiny'),device='cpu',compute_type='int8')
   segs,_=model.transcribe(str(src),language=language if language in ('en','hi') else None,beam_size=1)
   lines=[]
   for n,s in enumerate(segs,1):
    def ts(v):
     h=int(v//3600);m=int(v%3600//60);sec=int(v%60);ms=int((v%1)*1000)
     return f'{h:02}:{m:02}:{sec:02},{ms:03}'
    lines.extend([str(n),f'{ts(s.start)} --> {ts(s.end)}',s.text.strip(),''])
   (folder/'captions.srt').write_text('\n'.join(lines),encoding='utf-8')
   vf+=',subtitles='+str(folder/'captions.srt')
  jobs[jid]['message']='Rendering vertical MP4'
  output=folder/'clip.mp4'
  run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(offset),'-i',str(src),'-t',str(duration),'-vf',vf,'-c:v','libx264','-preset','ultrafast','-crf','28','-pix_fmt','yuv420p','-c:a','aac','-b:a','96k','-movflags','+faststart',str(output)],timeout=360)
  if not output.exists() or output.stat().st_size<1000:raise RuntimeError('Rendering produced no output')
  expiry=int(time.time()+1800)
  jobs[jid].update(status='done',message='Ready',url=f'/api/download/{jid}?token={token(jid,expiry)}')
 except Exception as e:jobs[jid].update(status='error',message=str(e)[:500])
 finally:slot.release()
@app.get('/api/health')
def health():return {'status':'ok','key_configured':bool(KEY)}
@app.post('/api/create')
async def create(file:UploadFile|None=File(default=None),videoUrl:str=Form(default=''),start:float=Form(default=0),length:float=Form(default=30),captions:str=Form(default='none'),custom:str=Form(default=''),language:str=Form(default='auto'),x_clipforge_key:str|None=Header(default=None)):
 auth(x_clipforge_key)
 if start<0 or length<20 or length>45:raise HTTPException(400,'Use a nonnegative start and 20-45 second duration')
 if captions not in ('none','custom','auto'):raise HTTPException(400,'Invalid caption mode')
 if (not file and not videoUrl) or (file and videoUrl):raise HTTPException(400,'Provide exactly one video source')
 if videoUrl and not youtube(videoUrl):raise HTTPException(400,'Invalid YouTube link')
 if not slot.acquire(blocking=False):raise HTTPException(429,'Processor busy; retry later')
 jid=uuid.uuid4().hex;folder=DATA/jid;folder.mkdir()
 try:
  source=None
  if file:
   source=folder/'input.mp4';size=0
   with source.open('wb') as dest:
    while True:
     chunk=await file.read(1024*1024)
     if not chunk:break
     size+=len(chunk)
     if size>65*1024*1024:raise HTTPException(413,'Maximum upload size is 65 MB')
     dest.write(chunk)
   await file.close()
  jobs[jid]={'status':'queued','message':'Queued','created':time.time()}
  threading.Thread(target=work,args=(jid,source,videoUrl,start,length,captions,custom,language),daemon=True).start()
  return {'jobId':jid}
 except:
  shutil.rmtree(folder,ignore_errors=True);slot.release();raise
@app.get('/api/status/{jid}')
def status(jid:str,x_clipforge_key:str|None=Header(default=None)):
 auth(x_clipforge_key)
 if jid not in jobs:raise HTTPException(404,'Unknown job')
 j=jobs[jid]
 if time.time()-j['created']>3600:raise HTTPException(410,'Job expired')
 return {k:v for k,v in j.items() if k!='created'}
@app.get('/api/download/{jid}')
def download(jid:str,token:str):
 try:
  expires,mac=token.split('.',1)
  if int(expires)<time.time() or not secrets.compare_digest(token,globals()['token'](jid,int(expires))):raise ValueError()
 except:raise HTTPException(403,'Expired or invalid download link')
 path=DATA/jid/'clip.mp4'
 if not path.exists():raise HTTPException(404,'Clip not available')
 return FileResponse(path,media_type='video/mp4',filename='clipforge.mp4')
