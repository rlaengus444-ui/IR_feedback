"""Reproduce a causal offline synthetic comparison; no hardware or Excel use."""
from pathlib import Path
import argparse, csv, hashlib, importlib.util, itertools, json, math, sys, zipfile
from dataclasses import replace
import numpy as np
from PIL import Image
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

APP = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(APP))
from layer_algorithm import LayerAdaptiveController, LayerSettings
from image_guard import ImageGuardSettings
from slice_data import SliceConfig, analyze

BASE_HASH = 'c58716206a14dab21239df024746a596828205084676b537fef47a0edf7ffc19'
DEFAULT = dict(layers=300, target=65, initial_ir=60, gain=.6, thermal_tau=24, surface_tau=3,
               queue_delay=2, transient=6, sustained=2, decay=16, wetting_tau=1,
               carry=.85, noise_sd=.15, seed=42)
RANGES = ((41,100),(161,220))

def sha(data): return hashlib.sha256(data).hexdigest()

def load_baseline(path):
    with zipfile.ZipFile(path) as z:
        matches = [n for n in z.namelist() if n.endswith('/app/layer_algorithm.py') or n == 'app/layer_algorithm.py']
        if len(matches) != 1: raise ValueError('baseline zip must contain exactly one app/layer_algorithm.py')
        source = z.read(matches[0])
    if sha(source) != BASE_HASH: raise ValueError('baseline algorithm hash mismatch')
    module = type(sys)('released_layer_algorithm')
    sys.modules[module.__name__] = module
    exec(compile(source, 'pinned-release/layer_algorithm.py', 'exec'), module.__dict__)
    return module

def load_v2():
    path = Path(__file__).parent / 'reference' / 'limiter_v2.py'
    source = path.read_bytes()
    expected = 'ca52ae7913e1488a773d62b07aa0c5f64356a88e8dcd2cc2ac8876cdb009a83f'
    if sha(source) != expected: raise ValueError('v2 reference hash mismatch')
    module = type(sys)('research_v2_layer_algorithm'); sys.modules[module.__name__] = module
    exec(compile(source, str(path), 'exec'), module.__dict__)
    return module

def load_v3():
    path = Path(__file__).parent / 'reference' / 'limiter_v3.py'
    source = path.read_bytes()
    if sha(source) != 'fce0ecbb5c66f5509a5109bb90a1337376967dad482a1e949b7d88f31f475a2a':
        raise ValueError('v3 reference hash mismatch')
    module = type(sys)('research_v3_layer_algorithm'); sys.modules[module.__name__] = module
    exec(compile(source, str(path), 'exec'), module.__dict__)
    return module

def is_print(layer): return any(start <= layer <= end for start,end in RANGES)

def generate_images(output):
    folder = output / 'synthetic_slices'; folder.mkdir(parents=True, exist_ok=True)
    y,x = np.ogrid[:512,:512]
    disk = (x+.5-256)**2+(y+.5-256)**2 <= (3*400/25.4)**2
    for layer in range(1,301):
        a = np.zeros((512,512), dtype=np.uint8)
        if is_print(layer): a[disk] = 255
        Image.fromarray(a).save(folder / f'layer_{layer:03}.png', dpi=(400,400))
    dataset = analyze(sorted(folder.glob('*.png')), SliceConfig(radius_mm=5,dpi_x=400,dpi_y=400))
    dataset.export_csv(output / 'image_features.csv')
    return dataset

def image_features(layer, scenario, dataset):
    if scenario == 'missing' or (scenario == 'missing_mid_protection' and 44 <= layer <= 75) or (scenario == 'missing_after_last_print' and layer >= 221):
        f = dataset.features(layer)
        for prefix in ('','previous_'):
            f[prefix+'image_valid'] = 0
            for field in ('center_coverage_pct','center_intensity_pct','whole_coverage_pct'): f[prefix+field] = None
        return f
    mapped = max(1, layer - 5) if scenario == 'shifted' else layer
    f = dataset.features(mapped)
    if scenario == 'load_release':
        for prefix, image_layer in (('', layer), ('previous_', layer-1)):
            if image_layer >= 101 and f[prefix+'image_valid']:
                for field in ('center_coverage_pct','center_intensity_pct','whole_coverage_pct'): f[prefix+field] = 0
    if scenario == 'wrong_blank':
        for prefix in ('','previous_'):
            if f[prefix+'image_valid']:
                for field in ('center_coverage_pct','center_intensity_pct','whole_coverage_pct'): f[prefix+field] = 0
    if scenario == 'outer_only':
        for prefix in ('','previous_'):
            if f[prefix+'image_valid'] and f[prefix+'whole_coverage_pct'] > 0:
                f[prefix+'center_coverage_pct'] = f[prefix+'center_intensity_pct'] = 0
    if scenario == 'continuous_print' or (scenario == 'permanent_cooling' and layer >= 41):
        for prefix in (('',) if scenario == 'permanent_cooling' and layer == 41 else ('','previous_')):
            if f[prefix+'image_valid']:
                f[prefix+'whole_coverage_pct'] = 10
                f[prefix+'center_coverage_pct'] = f[prefix+'center_intensity_pct'] = 10
    return f

def simulate(module, settings, params, dataset, mode='image', scenario='physical_transient', guard=None):
    controller = module.LayerAdaptiveController(settings) if mode == 'base' else module.LayerAdaptiveController(settings, guard)
    noise = np.random.default_rng(params['seed']).normal(0,params['noise_sd'],params['layers'])
    commands = [params['initial_ir']] * (params['queue_delay'] + 1)
    h = surface = carry = bias_carry = 0.; rows = []
    ah = math.exp(-1/params['thermal_tau']); a_s = math.exp(-1/params['surface_tau']); aw = math.exp(-1/params['wetting_tau'])
    for k in range(1, params['layers']+1):
        start = next((s for s,e in RANGES if s<=k<=e), None)
        on = start is not None
        if scenario == 'continuous_print': on, start = True, 1
        if scenario == 'dry': on, start = False, None
        if scenario == 'load_release' and k >= 101: on, start = False, None
        if scenario == 'permanent_cooling' and k >= 41: on, start = True, 41
        demand = 0
        if on:
            transient = params['transient']
            if scenario in ('finite_sustained_print', 'permanent_cooling', 'disturbance_persists_after_print', 'load_release'): demand = params['sustained'] + transient
            elif scenario == 'gradual': demand = params['sustained'] + transient * (1-math.exp(-(k-start)/16))
            else: demand = params['sustained'] + transient * math.exp(-(k-start)/params['decay'])
        if scenario == 'disturbance_persists_after_print' and k >= 41: demand = params['sustained'] + params['transient']
        actual_demand = 0 if scenario in ('sensor_bias', 'noise_only', 'positive_outlier', 'negative_outlier') else demand
        next_carry = aw*carry+(1-aw)*actual_demand
        physical = params['target'] + surface - params['carry']*carry
        sensor = physical + float(noise[k-1])
        if scenario == 'sensor_bias': sensor -= params['carry'] * bias_carry
        if scenario == 'negative_outlier' and k in (42, 162): sensor -= 8
        if scenario == 'positive_outlier' and k in (40, 160): sensor += 4
        previous = commands[-1]
        kw = {'slice_features': image_features(k, scenario, dataset)} if mode == 'image' else {}
        r = controller.process(k, sensor, previous, **kw); u = float(r.power_percent)
        commands.append(u); effective = commands[-(params['queue_delay']+1)]
        d = r.diagnostics
        rows.append(dict(layer=k, binder=int(on), sensor=sensor, physical=physical, previous_ir=previous, ir=u,
                         G=d['L'], alpha=d['J'], beta=d['K'], state=d.get('CL',0), age=d.get('CI',0),
                         unlimited=d.get('CQ',u), timeout=d.get('CM',0), trigger=d.get('CH',0),
                         **{col:d.get(col) for col in ('CU','CV','CW','CJ','CG','CX','CE','CF','CP','CY','CZ','DA','AG','AM','AV','AW','AR','DC','DD','DE','DF','DG','DH','DI','DJ','DK','DL','DM','DN','DO','DP','DQ','DR')}))
        old = h; h = ah*h+(1-ah)*params['gain']*(2 if scenario == 'gain_step' and k >= 70 else 1)*(effective-params['initial_ir']); surface = a_s*surface+(1-a_s)*old; carry = next_carry; bias_carry = aw*bias_carry+(1-aw)*demand
        assert math.isfinite(u) and 0<=u<=100 and abs(u-previous)<=5+1e-9
    return rows

def metrics(rows, target=65):
    sensor = np.array([r['sensor'] for r in rows]); physical = np.array([r['physical'] for r in rows]); e = sensor-target
    # First five consecutive observations within ±0.5 C after each print start and before the next print start (or end of run); null if none.
    recovery = {}
    for index,(start,_) in enumerate(RANGES):
        end = RANGES[index+1][0]-1 if index+1 < len(RANGES) else len(rows)
        recovery[str(start)] = next((i-start for i in range(start, end-3) if all(abs(rows[j-1]['sensor']-target)<=.5 for j in range(i,i+5))), None)
    return dict(mae=float(np.mean(abs(e))), minimum=float(min(sensor)), maximum=float(max(sensor)),
                overshoot=float(max(0,max(e))), undershoot=float(max(0,-min(e))), absolute_integrated_error=float(sum(abs(e))),
                physical_mae=float(np.mean(abs(physical-target))), physical_min=float(min(physical)), physical_max=float(max(physical)), physical_overshoot=float(max(0,max(physical-target))), physical_undershoot=float(max(0,max(target-physical))),
                recovery_layers_5_consecutive_within_0_5=recovery,
                saturation_count=sum(r['ir'] <= 0 or r['ir'] >= 100 for r in rows),
                protection_count=sum(r['state'] != 0 for r in rows), trigger_count=sum(r['trigger'] for r in rows),
                timeout_count=sum(r['timeout'] for r in rows))

def comparison(base, v3, settings, base_settings, params, dataset, scenario, guard):
    current = sys.modules['layer_algorithm']
    optional = replace(guard, exclude_print_learning_after_event=True)
    old = replace(guard, absolute_recovery=False, exclude_print_learning_after_event=False)
    return {
        'base': simulate(base, base_settings, params, dataset, 'base', scenario),
        'v3': simulate(v3, settings, params, dataset, 'image', scenario, old),
        'absolute': simulate(current, settings, params, dataset, 'image', scenario, guard),
        'absolute_print_exclusion_research': simulate(current, settings, params, dataset, 'image', scenario, optional),
    }

def first_difference(base, candidate):
    return next((a['layer'] for a,b in zip(base,candidate) if any(a[k] != b[k] for k in ('sensor','ir','G','alpha','beta'))), None)

def summary(trajectories):
    return {name:dict(metrics(rows), first_divergence_from_base=first_difference(trajectories['base'],rows),
                      first_trigger=next((r['layer'] for r in rows if r['trigger']),None))
            for name,rows in trajectories.items()}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--baseline-zip',required=True); ap.add_argument('--output',required=True); args=ap.parse_args()
    out=Path(args.output); out.mkdir(parents=True,exist_ok=False)
    holdout = dict(gain=[.3,.55,.85],tau=[14,28,44],delay=[0,3],transient=[4,7,10],seed=[733,1559],cases=108,
                   additional_scenarios=['permanent_cooling','disturbance_persists_after_print','load_release','gain_step','slow_carry','sensor_bias','noise_only'],
                   controls='production defaults; no tuning against holdout')
    (out/'holdout_preregistration.json').write_text(json.dumps(holdout,indent=2))
    base=load_baseline(args.baseline_zip);v3=load_v3()
    doc=json.loads((APP/'workbook-replay.settings.json').read_text())
    base_settings=base.LayerSettings.from_dict(doc);settings=LayerSettings.from_dict(doc)
    guard=ImageGuardSettings.from_dict(doc)
    if guard.exclude_print_learning_after_event or not guard.absolute_recovery: raise ValueError('production default must have absolute recovery on and print-learning exclusion off')
    dataset=generate_images(out)
    nominal=comparison(base,v3,settings,base_settings,DEFAULT,dataset,'physical_transient',guard)
    nominal['noimage']=simulate(sys.modules['layer_algorithm'],settings,DEFAULT,dataset,'noimage',guard=guard)
    assert first_difference(nominal['base'],nominal['noimage']) is None
    dry=simulate(base,base_settings,DEFAULT,dataset,'base','dry')
    assert all(nominal['base'][i]['sensor']==dry[i]['sensor'] and nominal['base'][i]['ir']==dry[i]['ir'] for i in range(41))
    assert nominal['base'][41]['sensor'] != dry[41]['sensor']
    bm=metrics(nominal['base']);assert abs(bm['maximum']-69.373017)<1e-6 and abs(bm['minimum']-59.024039)<1e-6
    nominal_summary=summary(nominal)
    (out/'nominal_metrics.json').write_text(json.dumps(nominal_summary,indent=2))
    print(json.dumps(nominal_summary,indent=2),flush=True)
    rows=[]
    for i in range(300):
        row={'layer':i+1,'binder':nominal['base'][i]['binder']}
        for name,trajectory in nominal.items():
            for field,value in trajectory[i].items():
                if field not in ('layer','binder'): row[name+'_'+field]=value
        rows.append(row)
    with (out/'simulation_300.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,rows[0]);writer.writeheader();writer.writerows(rows)
    fig,axes=plt.subplots(3,1,figsize=(13,10),sharex=True,constrained_layout=True)
    fontpath='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc';font=font_manager.FontProperties(fname=fontpath) if Path(fontpath).exists() else None
    for ax in axes:
        for a,b in RANGES: ax.axvspan(a,b,color='orange',alpha=.12)
        ax.grid(alpha=.2)
    for name,color,label in [('base','#bc4343','기존'),('v3','#a87334','v3 참고'),('absolute','#26669c','절대값 회복'),('absolute_print_exclusion_research','#7958a6','학습 제외 연구')]:
        data=nominal[name];axes[0].plot(range(1,301),[r['sensor'] for r in data],label=label,color=color,alpha=.8);axes[1].plot(range(1,301),[r['ir'] for r in data],label=label,color=color,alpha=.8)
    axes[0].axhspan(64.5,65.5,color='green',alpha=.12,label='목표 ±0.5 °C');axes[0].axhline(65,color='green',ls='--');axes[0].set_ylabel('온도 °C',fontproperties=font)
    axes[1].set_ylabel('IR %',fontproperties=font);axes[2].step(range(1,301),[r['state'] for r in nominal['absolute']],label='이미지 억제 상태',where='mid');axes[2].set_ylabel('상태',fontproperties=font);axes[2].set_xlabel('층',fontproperties=font)
    for ax in axes: ax.legend(prop=font)
    fig.suptitle('300층 합성 오프라인 비교',fontproperties=font);fig.savefig(out/'simulation_300.png',dpi=180);fig.savefig(out/'simulation_300.pdf');plt.close(fig)
    scenarios=['physical_transient','finite_sustained_print','gradual','dry','continuous_print','wrong_blank','shifted','missing','missing_mid_protection','missing_after_last_print','outer_only','positive_outlier','negative_outlier','delay5']
    results=[]
    for scenario in scenarios + holdout['additional_scenarios']:
        p=dict(DEFAULT)
        if scenario=='delay5':p['queue_delay']=5
        if scenario=='slow_carry':p.update(wetting_tau=4,carry=.85)
        ts=comparison(base,v3,settings,base_settings,p,dataset,scenario,guard)
        for name in ('absolute','absolute_print_exclusion_research'):
            first_trigger=next((r['layer'] for r in ts[name] if r['trigger']),301)
            assert all(all(a[k]==b[k] for k in ('sensor','ir','G','alpha','beta')) for a,b in zip(ts['base'][:first_trigger-1],ts[name][:first_trigger-1])),(scenario,name,'pre-event mismatch')
        if scenario in ('noise_only','dry'):
            assert all(first_difference(ts['base'],ts[name]) is None and not any(r['trigger'] for r in ts[name]) for name in ('absolute','absolute_print_exclusion_research'))
        results.append(dict(scenario=scenario,split='fresh_holdout_scenario' if scenario in holdout['additional_scenarios'] else 'development',parameters=p,metrics=summary(ts)))
    for gain,tau,delay,cooling,seed in itertools.product((.35,.6,.9),(12,24,40),(0,2,4),(3,6,9),(42,127)):
        p=dict(DEFAULT,gain=gain,thermal_tau=tau,queue_delay=delay,transient=cooling,seed=seed)
        results.append(dict(scenario='physical_transient',split='development_regression_grid',parameters=p,metrics=summary(comparison(base,v3,settings,base_settings,p,dataset,'physical_transient',guard))))
    for gain,tau,delay,cooling,seed in itertools.product((.45,.75),(18,32),(1,3),(4,8),(281,911)):
        p=dict(DEFAULT,gain=gain,thermal_tau=tau,queue_delay=delay,transient=cooling,seed=seed)
        results.append(dict(scenario='physical_transient',split='v2_regression_grid',parameters=p,metrics=summary(comparison(base,v3,settings,base_settings,p,dataset,'physical_transient',guard))))
    for gain,tau,delay,cooling,seed in itertools.product((.4,.7),(16,36),(1,4),(5,10),(517,1039)):
        p=dict(DEFAULT,gain=gain,thermal_tau=tau,queue_delay=delay,transient=cooling,seed=seed)
        results.append(dict(scenario='physical_transient',split='v3_regression_grid',parameters=p,metrics=summary(comparison(base,v3,settings,base_settings,p,dataset,'physical_transient',guard))))
    for gain,tau,delay,cooling,seed in itertools.product(holdout['gain'],holdout['tau'],holdout['delay'],holdout['transient'],holdout['seed']):
        p=dict(DEFAULT,gain=gain,thermal_tau=tau,queue_delay=delay,transient=cooling,seed=seed)
        results.append(dict(scenario='physical_transient',split='fresh_holdout',parameters=p,metrics=summary(comparison(base,v3,settings,base_settings,p,dataset,'physical_transient',guard))))
    (out/'scenario_metrics.json').write_text(json.dumps(results,indent=2))
    regressions={}
    for name in ('absolute','absolute_print_exclusion_research'):
        regressions[name]={}
        for metric in ('mae','overshoot','undershoot','absolute_integrated_error'):
            comparisons=[(r['metrics'][name][metric]-r['metrics']['base'][metric],r['parameters']) for r in results if r['split']=='fresh_holdout']
            worst=max(comparisons,key=lambda x:x[0]);regressions[name][metric]=dict(count=sum(d>1e-12 for d,_ in comparisons),worst_increase=worst[0],parameters=worst[1])
    (out/'metrics.json').write_text(json.dumps(dict(nominal=nominal_summary,heldout_regressions=regressions,heldout_grid_cases=108,feature_fingerprint=dataset.fingerprint),indent=2))
    paths=[APP/'layer_algorithm.py',APP/'image_guard.py',APP/'slice_data.py',APP/'workbook-replay.settings.json',APP/'templates'/'IR_슬라이싱_수동계산.xlsx',Path(__file__),Path(__file__).parent/'reference'/'limiter_v2.py',Path(__file__).parent/'reference'/'limiter_v3.py']
    (out/'parameters.json').write_text(json.dumps(dict(nominal=DEFAULT,ranges=RANGES,development_scenarios=scenarios,development_grid='162 previously inspected cases',v2_regression_grid='32 formerly held-out cases, now inspected',v3_regression_grid='32 formerly held-out cases, now inspected',fresh_holdout=holdout,production_guard=vars(guard),research_guard=vars(replace(guard,exclude_print_learning_after_event=True)),image='512px, 3mm white disk, 400 dpi, center radius 5mm',causality='temperature measured before current print; commands applied after measurement; no warmup',hashes={str(p):sha(p.read_bytes()) for p in paths},baseline_algorithm_sha256=BASE_HASH,baseline_zip_sha256=sha(Path(args.baseline_zip).read_bytes()),limitations='Synthetic model; no real equipment or Excel COM validation.'),indent=2))
if __name__=='__main__':main()
