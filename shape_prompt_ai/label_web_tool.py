"""Tap-to-label tool for building your own training set (stdlib only, runs in Termux).

    python label_web_tool.py --images images --tags tags
    then open  http://127.0.0.1:8765  in your phone browser.

Tap the 📁 icon in the top bar to browse the server's filesystem and switch the images/tags
folders without restarting - handy if --images doesn't exist yet or you want to relabel a
different set later. On --lan this lets anyone on your Wi-Fi browse folder names on this
device, same trust model as the rest of this tool.

For each image: tap tags, pick a rating (required), Save & Next. Output matches tagger_prep_dataset.py:
    tags/<image-stem>.txt   ->  "1girl, solo, black_hair, rating:general"
Suggestions from the built-in analyser (hair colour guess, background colour) are pre-highlighted;
tap to confirm or remove. Edit label_vocab.json to change the tag buttons. Type extra tags in the box.

Rating guide (this trains the moderation head):
  general = nothing suggestive | sensitive = swimwear, cleavage, mildly suggestive
  questionable = partial nudity / strongly suggestive | explicit = nudity or sexual content
Label honestly - the flagger can only be as good as these labels.

    python label_web_tool.py --images images --tags tags --stats     # progress + tag/rating counts
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
EXTS = (".jpg", ".jpeg", ".png", ".webp")
RATINGS = ["general", "sensitive", "questionable", "explicit"]
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

# Live server state. A plain dict (not closure locals) so the folder picker in the web UI
# can repoint the running server at a new images/tags folder without a restart.
STATE = {"images": None, "tags": None}


def clean_tag(t):
    return re.sub(r"[^a-z0-9_:\-()]", "", re.sub(r"\s+", "_", str(t).strip().lower()))


def read_label(tags_dir, stem):
    p = os.path.join(tags_dir, stem + ".txt")
    if not os.path.exists(p):
        return None
    parts = [x.strip() for x in re.split(r"[,\n]", open(p, encoding="utf-8").read()) if x.strip()]
    rating = next((x.split(":", 1)[1] for x in parts if x.startswith("rating:")), None)
    return [x for x in parts if not x.startswith("rating:")], rating


def save_label(tags_dir, stem, tags, rating):
    if rating not in RATINGS:
        raise ValueError("rating required")
    tags = sorted({clean_tag(t) for t in tags if clean_tag(t) and not clean_tag(t).startswith("rating:")})
    os.makedirs(tags_dir, exist_ok=True)
    with open(os.path.join(tags_dir, stem + ".txt"), "w", encoding="utf-8") as f:
        f.write(", ".join(tags + [f"rating:{rating}"]))


WEIGHTS = None   # set by --weights: the model pre-fills tags (bootstrap loop)


def model_suggestions(path):
    """Tags (and a rating hint) from your trained ViT. Returns (tags, rating_or_None)."""
    sys.path.insert(0, HERE)
    import tagger_predict
    on, ex, _ = tagger_predict.tag_image(path, WEIGHTS)
    tags = [t for t in on]
    for k, (v, p) in ex.items():
        if k == "subject_count" and v == "solo":
            tags += ["solo"]
        if k == "background":
            tags.append(f"{v}_background")
    rating = ex.get("rating", (None, 0))[0]
    return list(dict.fromkeys(tags)), rating


def suggestions(path):
    try:
        sys.path.insert(0, HERE)
        import image_to_prompt as IP
        _, info = IP.analyse_basic(path, guess=True)
        fix = {"dark red": "red", "dark gray": "grey", "gray": "grey", "navy": "blue", "magenta": "pink",
               "orange": "red", "olive": "green", "teal": "green"}
        out = [fix.get(c, c).replace(" ", "_") + "_hair" for c in info.get("hair_guess", [])]
        bg = info.get("background", "")
        if bg in ("white", "black", "red", "purple", "blue", "pink", "gray"):
            out.append(("grey" if bg == "gray" else bg) + "_background")
        return list(dict.fromkeys(out))
    except Exception:
        return []


PAGE = r"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>EMLII Labeler</title>
<style>
:root{
  --bg:#0d0f12; --panel:#15181c; --panel2:#1b1f24; --border:#2a2f36; --border2:#363c45;
  --text:#e8eaed; --muted:#8a919c; --accent:#3b82f6; --accent-dim:#1e3a5f;
  --sug:#f6ad55; --ok:#38a169; --g-general:#2f855a; --g-sensitive:#b7791f;
  --g-questionable:#c05621; --g-explicit:#c53030; --radius:14px;
}
*{box-sizing:border-box} body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:var(--bg);color:var(--text);padding-bottom:84px}

/* header */
#top{position:sticky;top:0;z-index:5;background:rgba(13,15,18,.96);backdrop-filter:blur(6px);
 padding:10px 12px 8px;border-bottom:1px solid var(--border)}
#topRow{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:13px;color:var(--muted)}
#fname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
#bar{height:5px;border-radius:3px;background:var(--panel2);margin-top:7px;overflow:hidden}
#barFill{height:100%;background:var(--accent);width:0;transition:width .2s}
.iconBtn{border:1px solid var(--border2);background:var(--panel2);color:var(--text);
 border-radius:8px;width:34px;height:34px;font-size:16px;flex:0 0 auto;line-height:1}

/* folder picker modal */
.modalBack{display:none;position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.6);align-items:flex-end;justify-content:center}
.modal{background:var(--panel);width:100%;max-width:520px;max-height:88vh;overflow-y:auto;
 border-radius:16px 16px 0 0;padding:16px;border:1px solid var(--border);border-bottom:0}
.modal h3{margin:0 0 12px}
.fieldGroup{margin-bottom:14px}
.fieldGroup label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
.pathRow{display:flex;gap:6px}
.pathRow input{font-family:ui-monospace,monospace;font-size:13px}
.hint{font-size:12px;color:var(--muted);margin-top:5px}
.modalErr{font-size:13px;color:#f87171;min-height:18px;margin:4px 0}
.modalActions{display:flex;gap:8px;margin-top:10px}
.browsePanel{margin:10px 0;border:1px solid var(--border2);border-radius:10px;overflow:hidden;background:var(--panel2)}
.crumb{padding:9px 11px;font-family:ui-monospace,monospace;font-size:12px;color:var(--sug);
 background:var(--panel);border-bottom:1px solid var(--border2);word-break:break-all}
.browseList{max-height:240px;overflow-y:auto}
.browseRow{padding:11px 13px;font-size:14px;border-bottom:1px solid var(--border);cursor:pointer}
.browseRow:last-child{border-bottom:0}
.browseRow:active{background:var(--panel)}
.browseActions{display:flex;gap:8px;padding:8px;background:var(--panel)}
.miniBtn.primary{background:var(--accent);border-color:#60a5fa;color:#fff}
.browseActions .miniBtn{flex:1;padding:9px;text-align:center}

/* image card */
#imWrap{margin:12px;border-radius:var(--radius);overflow:hidden;background:#000;border:1px solid var(--border)}
img{display:block;width:100%;max-height:52vh;object-fit:contain;background:#000}

/* suggestion strip */
#sugBar{margin:0 12px 4px;display:none;align-items:center;gap:8px;flex-wrap:wrap;
 background:linear-gradient(0deg,rgba(246,173,85,.08),rgba(246,173,85,.08));
 border:1px dashed var(--sug);border-radius:10px;padding:8px 10px}
#sugBar .lbl{font-size:12px;color:var(--sug);font-weight:600;margin-right:2px}
.miniBtn{border:1px solid var(--border2);background:var(--panel2);color:var(--text);
 border-radius:8px;padding:5px 10px;font-size:12px}

/* sections */
.card{margin:10px 12px;background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
.card summary{list-style:none;cursor:pointer;padding:11px 14px;display:flex;justify-content:space-between;
 align-items:center;font-size:14px;font-weight:600;color:#b9c2cf;user-select:none}
.card summary::-webkit-details-marker{display:none}
.card summary .count{font-weight:400;color:var(--muted);font-size:12px;background:var(--panel2);
 border-radius:10px;padding:2px 8px;margin-left:8px}
.card summary .chev{transition:transform .15s;color:var(--muted)}
.card[open] summary .chev{transform:rotate(90deg)}
.row{padding:2px 12px 12px;display:flex;flex-wrap:wrap;gap:7px}

/* tag chips */
.t{padding:9px 13px;min-height:38px;border-radius:18px;background:var(--panel2);border:1px solid var(--border2);
 color:var(--text);font-size:14px;line-height:1;white-space:nowrap}
.t.on{background:var(--accent);border-color:#60a5fa;color:#fff;font-weight:600}
.t.sug:not(.on){border-color:var(--sug);border-style:dashed;color:var(--sug)}

/* rating */
#ratingCard{margin:10px 12px 0;padding:12px;border-radius:var(--radius);background:var(--panel);border:1px solid var(--border)}
#ratingCard.missing{border-color:#7a2e2e}
#ratingHead{display:flex;justify-content:space-between;align-items:center;margin-bottom:9px}
#ratingHead b{font-size:14px}
#ratingHead .req{font-size:12px;color:#f87171}
.ratingGrid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.rbtn{padding:13px 8px;border-radius:10px;border:1px solid var(--border2);background:var(--panel2);
 color:var(--text);font-size:14px;font-weight:600;text-align:center}
.rbtn small{display:block;font-weight:400;color:var(--muted);font-size:11px;margin-top:2px}
.rbtn.sug{border-style:dashed;border-color:var(--sug)}
.rbtn[data-r=general].on{background:var(--g-general);border-color:#48bb78}
.rbtn[data-r=sensitive].on{background:var(--g-sensitive);border-color:#ecc94b}
.rbtn[data-r=questionable].on{background:var(--g-questionable);border-color:#ed8936}
.rbtn[data-r=explicit].on{background:var(--g-explicit);border-color:#fc8181}

/* extra tags */
#extraWrap{margin:14px 12px}
#extraWrap label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
input{padding:11px 12px;font-size:15px;width:100%;border-radius:10px;border:1px solid var(--border2);
 background:var(--panel2);color:var(--text)}

/* bottom action bar */
#actions{position:fixed;left:0;right:0;bottom:0;z-index:6;background:rgba(13,15,18,.97);backdrop-filter:blur(6px);
 border-top:1px solid var(--border);padding:10px 12px;display:flex;gap:8px}
.btn{flex:1;padding:14px 10px;border-radius:10px;border:0;font-size:15px;font-weight:600;color:#fff;background:var(--ok)}
.btn.g{background:var(--panel2);color:var(--text);border:1px solid var(--border2);flex:0 0 72px}
.btn:disabled{background:#2c3137;color:var(--muted)}
#done{padding:60px 20px;text-align:center}
#done h2{font-size:22px}
</style>

<div id=top>
 <div id=topRow>
  <button class=iconBtn onclick=openFolders() title="Choose folders">📁</button>
  <span id=fname>-</span><span id=prog>0/0</span>
 </div>
 <div id=bar><div id=barFill></div></div>
</div>

<div id=root></div>

<div id=actions>
 <button class="btn g" onclick=go(-1)>Back</button>
 <button class="btn g" onclick=go(1)>Skip</button>
 <button class=btn id=saveBtn onclick=save()>Save &amp; Next</button>
</div>

<div id=modalBack class=modalBack onclick="if(event.target===this)closeFolders()">
 <div class=modal>
  <h3>Folders</h3>
  <div class=fieldGroup>
   <label>Images folder (source photos)</label>
   <div class=pathRow><input id=imagesPath placeholder="/path/to/images"><button class=iconBtn onclick="browseOpen('images')">📂</button></div>
   <div id=imagesInfo class=hint></div>
  </div>
  <div class=fieldGroup>
   <label>Tags folder (where labels are saved)</label>
   <div class=pathRow><input id=tagsPath placeholder="/path/to/tags"><button class=iconBtn onclick="browseOpen('tags')">📂</button></div>
   <div class=hint>Created automatically if it doesn't exist yet.</div>
  </div>
  <div id=browsePanel class=browsePanel style="display:none">
   <div id=browseCrumb class=crumb></div>
   <div id=browseList class=browseList></div>
   <div class=browseActions>
    <button class=miniBtn onclick="browseClose()">Cancel</button>
    <button class="miniBtn primary" onclick="browseUse()">Use this folder</button>
   </div>
  </div>
  <div id=modalErr class=modalErr></div>
  <div class=modalActions>
   <button class="btn g" onclick="closeFolders()">Close</button>
   <button class=btn onclick="applyFolders()">Save &amp; Reload</button>
  </div>
 </div>
</div>

<script>
let L=[],i=0,sel=new Set(),sug=new Set(),rating=null,srat=null,V={};
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const label=t=>esc(t.replace(/_/g,' '));

async function init(){
  V=await (await fetch('/api/vocab')).json();
  L=await (await fetch('/api/list')).json();
  if(!L.length){
    $('#fname').textContent='-';$('#prog').textContent='0/0';
    $('#root').innerHTML='<div id=done><h2>No images found</h2><p style="color:var(--muted)">Pick an images folder to get started.</p></div>';
    openFolders();
    return;
  }
  i=Math.max(0,L.findIndex(x=>!x.done));
  load();
}

// ---- folder picker ----
let browseTarget=null, browsePath='';

async function openFolders(){
  $('#modalErr').textContent='';
  const d=await (await fetch('/api/dirs')).json();
  $('#imagesPath').value=d.images||'';
  $('#tagsPath').value=d.tags||'';
  $('#imagesInfo').textContent=d.image_count+' image'+(d.image_count===1?'':'s')+' found'+(d.images_exists?'':' (folder does not exist yet)');
  $('#modalBack').style.display='flex';
}
function closeFolders(){ $('#modalBack').style.display='none'; browseClose(); }

function browseOpen(target){
  browseTarget=target;
  const seed=(target==='images'?$('#imagesPath'):$('#tagsPath')).value;
  $('#browsePanel').style.display='block';
  browseTo(seed);
}
function browseClose(){ $('#browsePanel').style.display='none'; browseTarget=null; }

async function browseTo(path){
  const d=await (await fetch('/api/browse?path='+encodeURIComponent(path||''))).json();
  if(d.error){ $('#browseCrumb').textContent=d.error; $('#browseList').innerHTML=''; return; }
  browsePath=d.path;
  $('#browseCrumb').textContent=d.path+'  ('+d.image_count+' image'+(d.image_count===1?'':'s')+')';
  let h='';
  if(d.parent!==null) h+=`<div class=browseRow data-path="${esc(d.parent)}">⬆ ..</div>`;
  for(const e of d.entries) h+=`<div class=browseRow data-path="${esc(e.path)}">📁 ${esc(e.name)}</div>`;
  if(!d.entries.length && d.parent===null) h+='<div class=hint style="padding:10px">no subfolders here</div>';
  $('#browseList').innerHTML=h;
}
$('#browseList').addEventListener('click', e=>{
  const row=e.target.closest('.browseRow');
  if(row) browseTo(row.dataset.path);
});

function browseUse(){
  if(browseTarget==='images') $('#imagesPath').value=browsePath;
  else $('#tagsPath').value=browsePath;
  browseClose();
}

async function applyFolders(){
  const images=$('#imagesPath').value.trim(), tags=$('#tagsPath').value.trim();
  const r=await fetch('/api/set-dirs',{method:'POST',body:JSON.stringify({images,tags})});
  const d=await r.json();
  if(!r.ok){ $('#modalErr').textContent=d.error||'failed'; return; }
  closeFolders();
  init();
}

async function load(){
  const n=L[i];
  if(!n){
    $('#root').innerHTML='<div id=done><h2>All done</h2><p style="color:var(--muted)">Every image in this folder has a label.</p></div>';
    $('#fname').textContent='-';$('#prog').textContent=L.length+'/'+L.length;$('#barFill').style.width='100%';
    $('#saveBtn').disabled=true;
    return;
  }
  const d=await (await fetch('/api/item?name='+encodeURIComponent(n.name))).json();
  sel=new Set(d.tags||[]);
  sug=new Set(d.tags?[]:d.suggest);
  if(!d.tags) d.suggest.forEach(t=>sel.add(t));
  rating=d.rating; srat=d.suggest_rating;
  $('#fname').textContent=n.name;
  $('#im').src='/img/'+encodeURIComponent(n.name);
  const done=L.filter(x=>x.done).length;
  $('#prog').textContent=(i+1)+'/'+L.length+' · '+done+' done';
  $('#barFill').style.width=(100*(i)/Math.max(1,L.length-1))+'%';
  draw();
}

function draw(){
  // Full rebuild - only called when the item changes (load()) or on bulk accept-all.
  // Single-tag taps use tg()/tgFromSuggestion() below, which patch the DOM in place
  // instead of recreating every <details> node (recreating them mid-interaction is
  // what was causing sections to snap shut on tap).
  const known=new Set(Object.values(V).flat());
  const extra=[...sel].filter(t=>!known.has(t));
  let h='';
  if(extra.length) h+=section('other',extra,true,true);
  for(const [k,tags] of Object.entries(V)) h+=section(k,tags,false,true);
  $('#sections').innerHTML=h;
  refreshSugBar();
  drawRating();
}

function section(title,tags,isOther,open){
  const n=tags.filter(t=>sel.has(t)).length;
  return `<details class=card ${isOther?'data-kind="other"':''} ${open?'open':''}>
    <summary><span>${esc(title)} <span class=count>${n?n+' selected':tags.length}</span></span><span class=chev>›</span></summary>
    <div class=row>${tags.map(t=>
      `<button class="t ${sel.has(t)?'on':''} ${sug.has(t)?'sug':''}" data-tag="${esc(t)}" onclick="tg('${t}',this)">${label(t)}</button>`
    ).join('')}</div>
  </details>`;
}

function updateCardCount(card){
  const total=card.querySelectorAll('.row .t').length;
  const onCount=card.querySelectorAll('.row .t.on').length;
  card.querySelector('summary .count').textContent=onCount?onCount+' selected':total;
}

function refreshSugBar(){
  const pending=[...sug].filter(t=>!sel.has(t));
  if(pending.length){
    $('#sugBar').style.display='flex';
    $('#sugBar').innerHTML='<span class=lbl>suggested</span>'+
      pending.map(t=>`<button class=miniBtn onclick="tgFromSuggestion('${t}')">+ ${label(t)}</button>`).join('')+
      `<button class=miniBtn onclick="acceptAll()" style="border-color:var(--sug);color:var(--sug)">accept all</button>`;
  } else { $('#sugBar').style.display='none'; }
}

// Accept a suggestion by tag name (no element reference, e.g. from the suggestion strip).
// If a chip for it already exists in a category card, reuse tg() on that chip. Otherwise
// (a free-form suggestion with no matching vocab entry) create/extend the "other" card.
function tgFromSuggestion(t){
  const btn=document.querySelector('#sections .t[data-tag="'+CSS.escape(t)+'"]');
  if(btn){ tg(t,btn); return; }
  sel.add(t);
  let card=document.querySelector('#sections .card[data-kind=other]');
  if(!card){
    $('#sections').insertAdjacentHTML('afterbegin', section('other',[t],true,true));
  } else {
    card.querySelector('.row').insertAdjacentHTML('beforeend',
      `<button class="t on ${sug.has(t)?'sug':''}" data-tag="${esc(t)}" onclick="tg('${t}',this)">${label(t)}</button>`);
    updateCardCount(card);
  }
  refreshSugBar();
}

function drawRating(){
  const defs=[['general','nothing suggestive'],['sensitive','swimwear, mildly suggestive'],
              ['questionable','partial nudity / strongly suggestive'],['explicit','nudity or sexual content']];
  let h='';
  for(const [r,desc] of defs){
    const isSug=(!rating && srat===r);
    h+=`<button class="rbtn ${rating===r?'on':''} ${isSug?'sug':''}" data-r="${r}" onclick="rt('${r}')">
      ${r}${isSug?' <small>model guess</small>':`<small>${esc(desc)}</small>`}
    </button>`;
  }
  $('#ratingGrid').innerHTML=h;
  $('#ratingCard').classList.toggle('missing',!rating);
  $('#ratingReq').style.display=rating?'none':'inline';
}

function acceptAll(){ sug.forEach(t=>sel.add(t)); draw(); }

// Tap on a tag chip: toggle selection in place, never rebuild the whole section list.
// 'other' chips (free-form tags, see section()) are removed entirely when turned off
// instead of just dimmed, since they only exist in the DOM because they're selected.
function tg(t,btn){
  const nowOn=!sel.has(t);
  nowOn?sel.add(t):sel.delete(t);
  const card=btn?btn.closest('.card'):null;
  const isOtherCard=card&&card.hasAttribute('data-kind')&&card.dataset.kind==='other';
  if(isOtherCard&&!nowOn){
    const row=card.querySelector('.row');
    btn.remove();
    row.children.length?updateCardCount(card):card.remove();
  } else {
    if(btn) btn.classList.toggle('on',nowOn);
    if(card) updateCardCount(card);
  }
  refreshSugBar();
}
function rt(r){ rating=r; drawRating(); }
function go(d){ i=Math.min(L.length-1,Math.max(0,i+d)); load(); scrollTo(0,0); }

async function save(){
  if(!rating){ $('#ratingCard').classList.add('missing'); $('#ratingCard').scrollIntoView({behavior:'smooth',block:'center'}); return; }
  const extra=$('#extraInput').value.split(',').map(s=>s.trim()).filter(Boolean);
  $('#saveBtn').disabled=true; $('#saveBtn').textContent='Saving…';
  const r=await fetch('/api/save',{method:'POST',body:JSON.stringify({name:L[i].name,tags:[...sel,...extra],rating})});
  $('#saveBtn').disabled=false; $('#saveBtn').textContent='Save & Next';
  if(!r.ok){ alert('save failed'); return; }
  L[i].done=true; go(1);
}

// static shell for the parts that don't need to be rebuilt every draw()
$('#root').innerHTML=`
  <div id=imWrap><img id=im></div>
  <div id=sugBar></div>
  <div id=sections></div>
  <div id=ratingCard>
    <div id=ratingHead><b>Rating</b><span id=ratingReq class=req>required</span></div>
    <div class=ratingGrid id=ratingGrid></div>
  </div>
  <div id=extraWrap><label>+ extra tags (comma separated)</label><input id=extraInput placeholder="e.g. custom_tag, another_one"></div>
`;

init();
</script>"""


ORDER_SEED = 1   # None = alphabetical


def list_images(images):
    if not images or not os.path.isdir(images):
        return []
    names = sorted(f for f in os.listdir(images) if f.lower().endswith(EXTS))
    if ORDER_SEED is not None:      # fixed random order: your labelled subset is a fair sample, not one folder/artist
        import random
        random.Random(ORDER_SEED).shuffle(names)
    return names


def _image_count(folder):
    if not folder or not os.path.isdir(folder):
        return 0
    try:
        return sum(1 for f in os.listdir(folder) if f.lower().endswith(EXTS))
    except OSError:
        return 0


def browse_dir(raw_path):
    """List subfolders of raw_path for the in-browser folder picker. Directories only,
    hidden (dot) entries skipped. Returns a dict ready to JSON-encode."""
    base = (raw_path or "").strip() or STATE["images"] or os.path.expanduser("~")
    base = os.path.abspath(os.path.expanduser(base))
    if not os.path.isdir(base):
        fallback = os.path.expanduser("~")
        base = fallback if os.path.isdir(fallback) else os.getcwd()
    try:
        entries = []
        for e in sorted(os.scandir(base), key=lambda e: e.name.lower()):
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    entries.append({"name": e.name, "path": os.path.join(base, e.name)})
            except OSError:
                continue
        parent = os.path.dirname(base)
        if parent == base:
            parent = None
        return {"path": base, "parent": parent, "entries": entries, "image_count": _image_count(base)}
    except PermissionError:
        return {"error": f"permission denied: {base}"}


def make_handler():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _safe(self, name):
            name = os.path.basename(name)
            images = STATE["images"]
            return name if images and name.lower().endswith(EXTS) and os.path.isfile(os.path.join(images, name)) else None

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs, unquote
            u = urlparse(self.path)
            if u.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/vocab":
                return self._send(200, open(os.path.join(HERE, "label_vocab.json"), encoding="utf-8").read())
            if u.path == "/api/dirs":
                return self._send(200, json.dumps({
                    "images": STATE["images"], "tags": STATE["tags"],
                    "images_exists": bool(STATE["images"] and os.path.isdir(STATE["images"])),
                    "tags_exists": bool(STATE["tags"] and os.path.isdir(STATE["tags"])),
                    "image_count": _image_count(STATE["images"]),
                }))
            if u.path == "/api/browse":
                raw = parse_qs(u.query).get("path", [""])[0]
                return self._send(200, json.dumps(browse_dir(raw)))
            if u.path == "/api/list":
                names = list_images(STATE["images"])
                return self._send(200, json.dumps([{"name": n, "done": read_label(STATE["tags"], os.path.splitext(n)[0]) is not None} for n in names]))
            if u.path == "/api/item":
                n = self._safe(parse_qs(u.query).get("name", [""])[0])
                if not n:
                    return self._send(404, "{}")
                lab = read_label(STATE["tags"], os.path.splitext(n)[0])
                sug, srat = [], None
                if not lab:
                    p = os.path.join(STATE["images"], n)
                    if WEIGHTS:
                        try:
                            sug, srat = model_suggestions(p)
                        except Exception as e:
                            print("model suggestion failed:", e)
                    if not sug:
                        sug = suggestions(p)
                return self._send(200, json.dumps({"tags": lab[0] if lab else None, "rating": lab[1] if lab else None,
                                                   "suggest": sug, "suggest_rating": srat}))
            if u.path.startswith("/img/"):
                n = self._safe(unquote(u.path[5:]))
                if not n:
                    return self._send(404, b"", "text/plain")
                return self._send(200, open(os.path.join(STATE["images"], n), "rb").read(), MIME[os.path.splitext(n)[1].lower()])
            self._send(404, "{}")

        def do_POST(self):
            if self.path == "/api/set-dirs":
                try:
                    d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    images = os.path.abspath(os.path.expanduser(str(d.get("images", "")).strip()))
                    tags = os.path.abspath(os.path.expanduser(str(d.get("tags", "")).strip()))
                    if not os.path.isdir(images):
                        raise ValueError(f"images folder does not exist: {images}")
                    if not tags:
                        raise ValueError("tags folder path is required")
                    os.makedirs(tags, exist_ok=True)
                    STATE["images"], STATE["tags"] = images, tags
                    return self._send(200, json.dumps({"ok": True, "images": images, "tags": tags,
                                                        "image_count": _image_count(images)}))
                except Exception as e:
                    return self._send(400, json.dumps({"error": str(e)}))
            if self.path != "/api/save":
                return self._send(404, "{}")
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                n = self._safe(d["name"])
                if not n:
                    raise ValueError("bad name")
                save_label(STATE["tags"], os.path.splitext(n)[0], d.get("tags", []), d.get("rating"))
                self._send(200, '{"ok":true}')
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
    return H


def stats(images, tags_dir):
    names = sorted(f for f in os.listdir(images) if f.lower().endswith(EXTS))
    tc, rc, done = Counter(), Counter(), 0
    for n in names:
        lab = read_label(tags_dir, os.path.splitext(n)[0])
        if lab:
            done += 1
            tc.update(lab[0])
            rc[lab[1]] += 1
    print(f"labelled {done}/{len(names)}\nratings: {dict(rc)}\ntop tags: {tc.most_common(25)}")
    for r in ("questionable", "explicit"):
        if done and rc.get(r, 0) < 50:
            print(f"warning: only {rc.get(r, 0)} '{r}' examples - the flagger needs ~50+ of each to learn it "
                  f"(use --sorted or search your folder for such images and label them on purpose)")
    if done < 300:
        print("note: a few hundred+ labelled images are needed before the tagger is usable; thousands for good results.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default="images")
    ap.add_argument("--tags", default="tags")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--sorted", action="store_true", help="show images alphabetically instead of in a fixed random order")
    ap.add_argument("--weights", help="trained weights (.npz): the model pre-fills tags so you only correct mistakes")
    ap.add_argument("--lan", action="store_true", help="listen on your Wi-Fi so a phone can open it (only on a network you trust)")
    ap.add_argument("--open", action="store_true", help="open the browser automatically")
    a = ap.parse_args()
    images_abs = os.path.abspath(a.images)
    tags_abs = os.path.abspath(a.tags)
    if a.stats and not os.path.isdir(images_abs):
        sys.exit(f"no such folder: {a.images}")
    STATE["images"], STATE["tags"] = images_abs, tags_abs
    if not os.path.isdir(images_abs):
        print(f"note: '{a.images}' not found yet - use the folder icon in the web UI to pick one")
    if a.sorted:
        ORDER_SEED = None
    if a.weights:
        if not os.path.exists(a.weights):
            sys.exit(f"weights not found: {a.weights}")
        WEIGHTS = a.weights
        print("model suggestions ON (dashed = guess; the rating is never auto-selected)")
    if a.stats:
        stats(a.images, a.tags)
    else:
        host = "0.0.0.0" if a.lan else "127.0.0.1"
        url = f"http://127.0.0.1:{a.port}"
        if a.lan:
            import socket
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("10.255.255.255", 1))
                print(f"on your phone/other device open: http://{s.getsockname()[0]}:{a.port}")
                s.close()
            except OSError:
                print("LAN address not found; use your computer's IP address")
        print(f"labelling {a.images} -> {a.tags}\nopen {url}  (Ctrl+C to stop)")
        if a.open:
            import webbrowser
            webbrowser.open(url)
        try:
            HTTPServer((host, a.port), make_handler()).serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
