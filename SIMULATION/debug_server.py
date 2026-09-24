"""
Real-time Debug Server for V2V Simulation
Provides HTTP API and web dashboard to inspect vehicle state and knowledge.
"""
import json
import threading
import os
import psutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

_simulation_state = None
_server_thread = None
_httpd = None
_current_port = None
_enabled = False

DEFAULT_PORT = 8080
PORT_FILE = os.path.join(os.path.dirname(__file__), "debug_port.txt")

def is_enabled():
    return _enabled

def set_simulation_state(state):
    global _simulation_state
    if not _enabled:
        return
    _simulation_state = state


class DebugHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, default=str).encode())

    def _send_html(self, html):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == '/':
            self._serve_dashboard()
        elif path == '/api/status':
            self._api_status()
        elif path == '/api/vehicles':
            self._api_vehicles()
        elif path == '/api/vehicle':
            self._api_vehicle_detail(query.get('id', [None])[0])
        elif path == '/api/pairs':
            self._api_pairs()
        elif path == '/api/knowledge':
            self._api_knowledge(query.get('id', [None])[0])
        elif path == '/api/stats':
            self._api_stats()
        elif path == '/api/filter-stats':
            self._api_filter_stats()
        elif path == '/api/filter-rejects':
            self._api_filter_rejects()
        elif path == '/api/reroute-log':
            self._api_reroute_log()
        elif path == '/api/bifurcations':
            self._api_bifurcations()
        else:
            self._send_json({'error': 'Not found'}, 404)

    # ==================== API HANDLERS ====================

    def _api_status(self):
        if not _simulation_state:
            self._send_json({'error': 'No sim'}, 503)
            return
        from CLASS.Vehicle import IntelligentVehicle
        int_count = sum(1 for v in _simulation_state.vehicles.values() if isinstance(v, IntelligentVehicle))
        nrm_count = len(_simulation_state.vehicles) - int_count
        total_reroutes = sum(
            v.route_recalculations for v in _simulation_state.vehicles.values()
            if isinstance(v, IntelligentVehicle)
        )
        self._send_json({
            'step': _simulation_state.step,
            'vehicle_count': len(_simulation_state.vehicles),
            'intelligent_count': int_count,
            'normal_count': nrm_count,
            'sharing_events': _simulation_state.sharing_events,
            'pairs_in_range': len(_simulation_state.current_pairs_in_range),
            'active_shared_pairs': len(_simulation_state.shared_pairs),
            'total_reroutes': total_reroutes,
        })

    def _api_stats(self):
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        self._send_json({
            'ram_mb': round(mem_info.rss / (1024 * 1024), 2),
            'system_available_gb': round(psutil.virtual_memory().available / (1024 ** 3), 2),
        })

    def _api_filter_stats(self):
        from CLASS.VehicleKnowledge import VehicleKnowledge
        stats = VehicleKnowledge.get_filter_stats()
        active = VehicleKnowledge.get_active_filter_names()
        total_accepted = total_rejected = 0
        filters = []
        for name, counts in stats.items():
            a, r = counts["accepted"], counts["rejected"]
            t = a + r
            filters.append({"name": name, "accepted": a, "rejected": r,
                            "rejection_rate": round(r / t * 100, 1) if t else 0.0})
            total_accepted += a
            total_rejected += r
        self._send_json({"active_filters": active, "filters": filters,
                         "total_received": total_accepted + total_rejected,
                         "total_accepted": total_accepted, "total_rejected": total_rejected})

    def _api_filter_rejects(self):
        from CLASS.VehicleKnowledge import VehicleKnowledge
        self._send_json({"rejects": VehicleKnowledge.get_filter_reject_log()})

    def _api_reroute_log(self):
        from CLASS.Vehicle import IntelligentVehicle
        self._send_json({"events": IntelligentVehicle.reroute_log})

    def _api_bifurcations(self):
        from CLASS.Vehicle import IntelligentVehicle
        bifs = IntelligentVehicle._auto_bifurcations or set()
        self._send_json({"bifurcations": sorted(bifs), "count": len(bifs)})

    def _api_vehicles(self):
        if not _simulation_state:
            self._send_json({'error': 'No sim'}, 503)
            return
        from CLASS.Vehicle import IntelligentVehicle
        vehicles = []
        for veh_id, vehicle in _simulation_state.vehicles.items():
            is_int = isinstance(vehicle, IntelligentVehicle)
            entry = {
                'id': veh_id,
                'type': 'intelligent' if is_int else 'normal',
                'speed': round(vehicle.speed, 2) if vehicle.speed else 0,
                'current_edge': vehicle.current_segment_id,
                'sensor_count': sum(len(obs) for slots in vehicle.knowledge._sensors.values() for obs in slots.values()),
                'shared_count': sum(len(obs) for slots in vehicle.knowledge._shared.values() for obs in slots.values()),
            }
            if is_int:
                entry['reroutes'] = vehicle.route_recalculations
            vehicles.append(entry)
        self._send_json({'vehicles': vehicles, 'count': len(vehicles)})

    def _api_vehicle_detail(self, veh_id):
        if not _simulation_state:
            self._send_json({'error': 'No sim'}, 503)
            return
        if not veh_id or veh_id not in _simulation_state.vehicles:
            self._send_json({'error': f'Vehicle {veh_id} not found'}, 404)
            return

        vehicle = _simulation_state.vehicles[veh_id]
        partners = []
        for pair in _simulation_state.shared_pairs:
            if veh_id in pair:
                partner = [v for v in pair if v != veh_id][0]
                partners.append({
                    'id': partner,
                    'shared_at_step': _simulation_state.shared_pairs[pair],
                    'in_range': pair in _simulation_state.current_pairs_in_range,
                    'distance': round(_simulation_state.pair_distances.get(pair, -1), 1)
                })

        from CLASS.Vehicle import IntelligentVehicle
        is_int = isinstance(vehicle, IntelligentVehicle)
        result = {
            'id': veh_id,
            'type': 'intelligent' if is_int else 'normal',
            'position': vehicle.position,
            'speed': round(vehicle.speed, 2) if vehicle.speed else 0,
            'current_edge': vehicle.current_segment_id,
            'target_edge': vehicle.target_segment_id,
            'route': getattr(vehicle, 'current_route', []),
            'sharing_partners': partners,
            'knowledge_summary': {
                'sensors': {
                    'segments': list(vehicle.knowledge._sensors.keys()),
                    'total_observations': sum(len(obs) for slots in vehicle.knowledge._sensors.values() for obs in slots.values()),
                },
                'shared': {
                    'segments': list(vehicle.knowledge._shared.keys()),
                    'total_observations': sum(len(obs) for slots in vehicle.knowledge._shared.values() for obs in slots.values()),
                }
            }
        }
        if is_int:
            result['reroutes'] = vehicle.route_recalculations
            result['last_reroute_at'] = vehicle.last_recalculated_segment
            result['bifurcation_count'] = len(vehicle.bifurcation_segments)
            result['route_time_estimate'] = vehicle.get_route_time_estimate()
        self._send_json(result)

    def _api_pairs(self):
        if not _simulation_state:
            self._send_json({'error': 'No sim'}, 503)
            return
        pairs = []
        for pair, t in _simulation_state.shared_pairs.items():
            pair_list = list(pair)
            pairs.append({
                'vehicles': pair_list,
                'shared_at_step': t,
                'in_range': pair in _simulation_state.current_pairs_in_range,
                'distance': round(_simulation_state.pair_distances.get(pair, -1), 1)
            })
        self._send_json({'pairs': pairs,
                         'currently_in_range': len(_simulation_state.current_pairs_in_range),
                         'total_shared': len(_simulation_state.shared_pairs)})

    def _api_knowledge(self, veh_id):
        if not _simulation_state:
            self._send_json({'error': 'No sim'}, 503)
            return
        if not veh_id or veh_id not in _simulation_state.vehicles:
            self._send_json({'error': f'Vehicle {veh_id} not found'}, 404)
            return
        vehicle = _simulation_state.vehicles[veh_id]
        self._send_json({
            'vehicle_id': veh_id,
            'sensors': vehicle.knowledge._sensors,
            'shared': vehicle.knowledge._shared,
        })

    # ==================== DASHBOARD ====================

    def _serve_dashboard(self):
        self._send_html(DASHBOARD_HTML)


DASHBOARD_HTML = r'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>V2V Debug</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0a0f;color:#e0e0e0;padding:10px;font-size:13px}
h2{color:#aaa;font-size:.95em;margin:0 0 6px;padding-bottom:4px;border-bottom:1px solid #252535}
.bar{display:flex;gap:16px;background:#1a1a2e;padding:8px 14px;border-radius:6px;margin-bottom:8px;align-items:center;flex-wrap:wrap}
.s{text-align:center;min-width:48px}.sv{font-size:1.5em;font-weight:bold;color:#00d4ff}.sl{font-size:.68em;color:#888}
.grid{display:grid;gap:8px;height:calc(100vh - 105px)}
.p{background:#151520;border:1px solid #252535;border-radius:6px;padding:8px;display:flex;flex-direction:column;overflow:hidden;min-height:0}
.col{display:flex;flex-direction:column;gap:8px;min-height:0}
.scroll{flex:1;overflow-y:auto;min-height:0}
.vi{padding:5px 6px;border-bottom:1px solid #1a1a25;cursor:pointer;font-size:.85em}
.vi:hover{background:#1f1f30}.vi.sel{background:#252550;border-left:3px solid #00d4ff}
.vid{font-weight:bold;color:#00d4ff}
.vm{font-size:.78em;color:#555;margin-top:1px}
.b{display:inline-block;padding:1px 5px;border-radius:7px;font-size:.7em;margin-left:2px}
.bs{background:#2a5a2a;color:#6f6}.bh{background:#2a2a5a;color:#66f}
.sec{margin-top:10px}.sec h3{color:#ffa500;font-size:.88em;margin-bottom:4px}
.in-range{color:#6f6}.separated{color:#f66}
.mono{font-family:'Fira Code',Consolas,monospace;font-size:.8em}
.jn{margin-left:12px;border-left:1px solid #333;padding-left:5px}
.js{cursor:pointer;color:#66c2ff;padding:1px 2px;border-radius:2px;user-select:none}
.js:hover{background:#252540}
.ja{color:#888;font-size:.7em;display:inline-block;width:10px}
.jl{margin-left:12px;color:#c5c5c5;padding:1px 2px;border-left:1px solid #333;padding-left:5px}
.jk{color:#ffa500}.jv{color:#8dff8d}.jv.string{color:#ce9178}.jv.number{color:#b5cea8}
.jtb{display:flex;gap:5px;margin-bottom:5px;padding-bottom:5px;border-bottom:1px solid #333}
.jtb button{background:#252535;border:1px solid #404050;color:#aaa;padding:2px 7px;border-radius:3px;cursor:pointer;font-size:.73em}
.jtb button:hover{background:#353545;color:#fff}
.al{color:#666;font-size:.8em;margin-left:2px}.bk{color:#888}
.fbar{display:flex;gap:10px;background:#1a1a2e;padding:5px 12px;border-radius:6px;margin-bottom:6px;font-size:.82em;align-items:center;flex-wrap:wrap}
.ri{padding:4px 3px;border-bottom:1px solid #1a1a25;font-size:.85em}
.ti{color:#6f6;font-size:.7em;font-weight:bold}.tn{color:#f66;font-size:.7em;font-weight:bold}
</style></head><body>
<div class="bar">
 <div class="s"><div class="sv" id="step">-</div><div class="sl">Step</div></div>
 <div class="s"><div class="sv" id="vehs">-</div><div class="sl">Vehicles</div></div>
 <div class="s"><div class="sv" id="ic" style="color:#6f6;font-size:1.2em">-</div><div class="sl">INT</div></div>
 <div class="s"><div class="sv" id="nc" style="color:#f66;font-size:1.2em">-</div><div class="sl">NRM</div></div>
 <div style="border-left:1px solid #333;height:24px"></div>
 <div class="s"><div class="sv" id="shares">-</div><div class="sl">Shares</div></div>
 <div class="s"><div class="sv" id="pairs">-</div><div class="sl">Pairs</div></div>
 <div class="s"><div class="sv" id="rr" style="color:#ffa500">-</div><div class="sl">Reroutes</div></div>
 <div style="border-left:1px solid #333;height:24px"></div>
 <div class="s"><div class="sv" id="filt" style="color:#ffa500;font-size:1.1em">-</div><div class="sl">Filtered</div></div>
 <div class="s"><div class="sv" id="ram" style="color:#f66">-</div><div class="sl">RAM</div></div>
</div>
<div id="fbar" class="fbar" style="display:none"><b style="color:#ffa500">Filters:</b> <span id="fd"></span></div>

<div class="grid" style="grid-template-columns:260px 1fr 1fr 300px">
 <div class="p">
  <h2>Vehicles</h2>
  <div style="display:flex;gap:3px;margin-bottom:4px">
   <input id="vsrch" type="text" placeholder="Filter..." style="flex:1;background:#0d0d15;border:1px solid #333;color:#ddd;padding:3px 5px;border-radius:3px;font-size:.8em">
   <select id="vtype" style="background:#0d0d15;border:1px solid #333;color:#ddd;padding:3px;border-radius:3px;font-size:.8em">
    <option value="all">All</option><option value="intelligent">INT</option><option value="normal">NRM</option>
   </select>
  </div>
  <div class="scroll" id="vlist"></div>
 </div>
 <div class="p"><h2>Vehicle Detail</h2><div class="scroll" id="vdet"><p style="color:#555">Click a vehicle</p></div></div>
 <div class="p"><h2>Knowledge Data</h2><div class="scroll mono" id="kd" style="background:#0d0d15;padding:6px;border-radius:4px">Click a vehicle</div></div>
 <div class="col">
  <div class="p" style="flex:1"><h2 style="color:#ffa500">Rerouting</h2><div class="scroll mono" id="rlog"></div></div>
  <div class="p" style="flex:1"><h2 style="color:#f66">Filter Rejects</h2><div class="scroll mono" id="frej"></div></div>
 </div>
</div>
<script>
let sel=null,lastKD=null;
const SEP='||';
let xp=new Set(['sensors','shared']);

async function api(e){try{const r=await fetch('/api/'+e);if(!r.ok)return{error:r.status};return await r.json()}catch(x){return{error:x.message}}}

// JSON tree
function gvc(v){return v===null?'null':typeof v==='string'?'string':typeof v==='number'?'number':typeof v==='boolean'?'boolean':''}
function fv(v){return v===null?'null':typeof v==='string'?'"'+v+'"':String(v)}
function jn(k,v,p,pa){
 if(v!==null&&typeof v==='object'){
  const isA=Array.isArray(v),es=isA?[...v.entries()]:Object.entries(v),n=es.length;
  const ex=xp.has(p),ar=ex?'\u25BC':'\u25B6',br=isA?'[':'{';
  let h='<div class="jn"><div class="js" onclick="tg(\''+p.replace(/'/g,"\\'")+'\')">';
  h+='<span class="ja">'+ar+'</span> ';
  if(!pa)h+='<span class="jk">'+k+'</span>: ';
  h+='<span class="bk">'+br+'</span><span class="al">'+n+'</span></div>';
  if(ex){h+='<div>';if(!n)h+='<div class="jl" style="color:#666;font-style:italic">empty</div>';
  else for(const[ck,cv]of es)h+=jn(ck,cv,p+SEP+ck,isA);h+='</div>'}
  return h+'</div>';
 }
 const vc=gvc(v);
 const c=pa?'<span class="jv '+vc+'">'+fv(v)+'</span>':'<span class="jk">'+k+'</span>: <span class="jv '+vc+'">'+fv(v)+'</span>';
 return'<div class="jl">'+c+'</div>';
}
function tg(p){if(xp.has(p))xp.delete(p);else xp.add(p);rkd()}
function rkd(){
 const c=document.getElementById('kd');if(!lastKD||!c)return;
 let h='<div class="jtb"><button onclick="xpAll()">Expand All</button><button onclick="clAll()">Collapse All</button></div>';
 for(const[k,v]of Object.entries(lastKD))h+=jn(k,v,k);c.innerHTML=h;
}
function ca(o,pfx,s){if(o&&typeof o==='object'){for(const[k,v]of(Array.isArray(o)?[...o.entries()]:Object.entries(o))){const p=pfx?pfx+SEP+k:k;s.add(p);ca(v,p,s)}}}
function xpAll(){if(!lastKD)return;ca(lastKD,'',xp);rkd()}
function clAll(){xp.clear();rkd()}

async function refresh(){
 const[st,stats,fs,vehs,prs,rl,fr]=await Promise.all([
  api('status'),api('stats'),api('filter-stats'),api('vehicles'),api('pairs'),api('reroute-log'),api('filter-rejects')]);

 if(!st.error){
  document.getElementById('step').textContent=st.step;
  document.getElementById('vehs').textContent=st.vehicle_count;
  document.getElementById('ic').textContent=st.intelligent_count;
  document.getElementById('nc').textContent=st.normal_count;
  document.getElementById('shares').textContent=st.sharing_events;
  document.getElementById('pairs').textContent=st.active_shared_pairs;
  document.getElementById('rr').textContent=st.total_reroutes;
 }
 if(!stats.error)document.getElementById('ram').textContent=stats.ram_mb;
 if(!fs.error){
  document.getElementById('filt').textContent=(fs.total_rejected||0).toLocaleString();
  const bar=document.getElementById('fbar'),d=document.getElementById('fd');
  if(fs.filters.length){bar.style.display='flex';d.innerHTML=fs.filters.map(f=>f.name+': <span style="color:#6f6">'+f.accepted+'</span>/<span style="color:#f66">'+f.rejected+'</span> ('+f.rejection_rate+'%) ').join(' ')}
  else bar.style.display='none';
 }

 // Vehicles
 if(!vehs.error){
  const srch=document.getElementById('vsrch').value.toLowerCase();
  const tf=document.getElementById('vtype').value;
  let vl=vehs.vehicles;
  if(srch)vl=vl.filter(v=>v.id.toLowerCase().includes(srch));
  if(tf!=='all')vl=vl.filter(v=>v.type===tf);
  document.getElementById('vlist').innerHTML=vl.map(v=>{
   const tc=v.type==='intelligent'?'ti':'tn';
   const rr=v.reroutes!==undefined?' rr:'+v.reroutes:'';
   return'<div class="vi'+(sel===v.id?' sel':'')+'" onclick="sv(\''+v.id+'\')"><span class="vid">'+v.id+'</span> <span class="'+tc+'">'+(v.type==='intelligent'?'INT':'NRM')+'</span> <span class="b bs">'+v.sensor_count+'s</span><span class="b bh">'+v.shared_count+'sh</span><div class="vm">'+(v.current_edge||'?')+' '+v.speed+'m/s'+rr+'</div></div>';
  }).join('')||'<p style="color:#555">No vehicles match</p>';
 }

 // Pairs
 if(!prs.error){
  const el=document.getElementById('vlist').parentElement;
  // pairs handled in rlog area is fine
 }

 // Reroute log
 if(!rl.error&&rl.events){
  const evts=rl.events.slice(-50).reverse();
  document.getElementById('rlog').innerHTML=evts.length?evts.map(e=>{
   const rsn=e.reason||'';
   let tag=e.route_changed?'<span style="color:#6f6;font-weight:bold"> CHANGED</span>':'<span style="color:#888"> kept</span>';
   if(rsn==='no_knowledge')tag='<span style="color:#f66"> NO DATA</span>';
   else if(rsn==='knowledge_not_ahead')tag='<span style="color:#888"> NOT RELEVANT</span>';
   const ad=e.adapted_edges;
   const ads=typeof ad==='object'&&!Array.isArray(ad)?Object.entries(ad).map(([k,v])=>k+'='+v+'s').join(', '):(Array.isArray(ad)?ad.join(','):'');
   return'<div class="ri"><span style="color:#666">'+e.time+'s</span> <span class="vid">'+e.vehicle+'</span> @ <span style="color:#ffa500">'+e.bifurcation+'</span>'+tag+'<div style="color:#444;font-size:.88em">sens:['+e.sensor_segments.join(',')+'] shared:['+e.shared_segments.join(',')+(ads?'] tt:['+ads:'')+']</div></div>';
  }).join(''):'<p style="color:#555">No rerouting yet</p>';
 }

 // Filter rejects
 if(!fr.error&&fr.rejects){
  const items=fr.rejects.slice(-50).reverse();
  document.getElementById('frej').innerHTML=items.length?items.map(r=>'<div class="ri"><span style="color:#f66">'+r.filter+'</span> rej <span style="color:#ffa500">'+r.segment+'</span> s'+r.slot+' <span style="color:#66c2ff">'+r.vehicle+'</span> <span style="color:#444">spd='+((r.data||{}).speed||'?')+' cnt='+((r.data||{}).count||'?')+'</span></div>').join(''):'<p style="color:#555">No rejections</p>';
 }

 if(sel)await ld(sel);
}

async function sv(id){sel=id;await ld(id)}

async function ld(id){
 const[d,k]=await Promise.all([api('vehicle?id='+id),api('knowledge?id='+id)]);
 if(d.error){
  document.getElementById('vdet').innerHTML='<p style="color:#f66">Vehicle '+id+' gone</p>';
  sel=null;return;
 }
 const tc=d.type==='intelligent'?'#6f6':'#f66';
 let rr='';
 if(d.type==='intelligent')rr='<div class="sec"><h3>Rerouting</h3><p>Recalculations: <b>'+(d.reroutes||0)+'</b></p><p>Last at: '+(d.last_reroute_at||'none')+'</p><p>Bifurcations: '+(d.bifurcation_count||0)+'</p></div>';

 const route=d.route||[];
 const ci=route.indexOf(d.current_edge);
 const rhtml=route.map((e,i)=>{
  let s='color:#444';
  if(i===ci)s='color:#00d4ff;font-weight:bold';
  else if(i>ci)s='color:#888';
  return'<span style="'+s+'">'+e+'</span>';
 }).join(' \u2192 ');

 // Route time estimate section
 let rteHtml='';
 if(d.route_time_estimate){
  const rte=d.route_time_estimate;
  const srcColor={sensor:'#8dff8d',shared:'#66aaff','sensor+shared':'#ffd700',unknown:'#444'};
  const rows=rte.edges.map(e=>{
   const ttStr=e.tt!==null?e.tt+'s':'?';
   const sc=srcColor[e.source]||'#888';
   const known=e.tt!==null;
   return'<tr style="border-bottom:1px solid #1a1a25"><td style="color:'+(known?'#ccc':'#444')+'">'+e.edge+'</td>'+
    '<td style="color:'+(known?'#fff':'#444')+';text-align:right">'+ttStr+'</td>'+
    '<td style="color:'+sc+';font-size:.78em;padding-left:6px">'+e.source+'</td></tr>';
  }).join('');
  const totalStr=rte.total_known_s>0?rte.total_known_s+'s':'--';
  const coverage=rte.known_edges+rte.unknown_edges>0?Math.round(rte.known_edges/(rte.known_edges+rte.unknown_edges)*100)+'%':'0%';
  rteHtml='<div class="sec"><h3>Route Time Estimate <span style="color:#888;font-size:.85em;font-weight:normal">('+coverage+' coverage)</span></h3>'+
   '<p style="color:#00d4ff;font-size:1.1em;font-weight:bold;margin-bottom:4px">'+
   '\u2211 known: '+totalStr+'</p>'+
   '<table style="width:100%;border-collapse:collapse;font-size:.82em">'+
   '<tr style="color:#555;font-size:.78em"><th style="text-align:left">Edge</th><th style="text-align:right">TT</th><th style="text-align:left;padding-left:6px">Source</th></tr>'+
   rows+'</table>'+
   '<p style="color:#555;font-size:.78em;margin-top:4px">'+rte.known_edges+' known / '+rte.unknown_edges+' unknown edges ahead</p></div>';
 }

 document.getElementById('vdet').innerHTML=
  '<div class="sec"><h3>State <span style="color:'+tc+';font-weight:bold">'+d.type.toUpperCase()+'</span></h3>'+
  '<p>Speed: '+d.speed+' m/s</p>'+
  '<p>Edge: '+(d.current_edge||'?')+'</p>'+
  '<p>Target: '+(d.target_edge||'?')+'</p>'+
  '<p style="font-size:.83em;margin-top:3px">Route: '+rhtml+'</p></div>'+
  rteHtml+rr+
  '<div class="sec"><h3>Partners ('+d.sharing_partners.length+')</h3>'+
  (d.sharing_partners.map(p=>'<div style="padding:2px 0;border-bottom:1px solid #1a1a25"><span class="'+(p.in_range?'in-range':'separated')+'">'+(p.in_range?'\u25CF':'\u25CB')+'</span> '+p.id+' t='+p.shared_at_step+'s '+p.distance+'m</div>').join('')||'<span style="color:#555">None</span>')+
  '</div><div class="sec"><h3>Knowledge</h3>'+
  '<p>Sensors: '+d.knowledge_summary.sensors.total_observations+' obs / '+d.knowledge_summary.sensors.segments.length+' segs ['+d.knowledge_summary.sensors.segments.join(', ')+']</p>'+
  '<p>Shared: '+d.knowledge_summary.shared.total_observations+' obs / '+d.knowledge_summary.shared.segments.length+' segs ['+d.knowledge_summary.shared.segments.join(', ')+']</p></div>';

 if(!k.error){lastKD={sensors:k.sensors,shared:k.shared};rkd()}
 else document.getElementById('kd').innerHTML='<p style="color:#555">No data</p>';
}

refresh();setInterval(refresh,500);
</script></body></html>'''


def start_debug_server(state, port=DEFAULT_PORT, max_tries=10, enabled=True):
    global _server_thread, _httpd, _current_port, _enabled
    if not enabled:
        _enabled = False
        return False
    _enabled = True
    set_simulation_state(state)
    for offset in range(max_tries):
        try_port = port + offset
        try:
            _httpd = HTTPServer(('localhost', try_port), DebugHandler)
            _server_thread = threading.Thread(target=_httpd.serve_forever, daemon=True)
            _server_thread.start()
            _current_port = try_port
            if state is not None:
                setattr(state, "debug_port", try_port)
            try:
                with open(PORT_FILE, "w", encoding="utf-8") as f:
                    f.write(str(try_port))
            except OSError:
                pass
            print(f"[OK] Debug server started at http://localhost:{try_port}")
            return True
        except OSError:
            continue
    print(f"[WARN] Could not start debug server on ports {port}-{port+max_tries-1}")
    return False


def stop_debug_server():
    global _httpd, _current_port, _enabled
    if not _enabled:
        return
    if _httpd:
        _httpd.shutdown()
        _httpd.server_close()
        _httpd = None
        _current_port = None
        try:
            if os.path.exists(PORT_FILE):
                os.remove(PORT_FILE)
        except OSError:
            pass
        print("[OK] Debug server stopped")
