#!/usr/bin/env python3
import sys, os, traceback, tempfile, json
from pathlib import Path

os.chdir('/home/qianyu/fpl26_optimization_contest')
sys.path.insert(0, '.')

from optimizer.state import (
    OptimizerState, ControlState, ContextState, ModelState,
    TimingState, IterationState,
    CriticalPathEntry, PathNode, ClockDomainInfo,
)
from optimizer.pure.tool_filter import LoopPhase
from optimizer.pure.state_space import build_state_space, format_state_space_for_llm
from optimizer.pure.design_data import DesignDataManager

run_dir = Path(tempfile.mkdtemp(prefix='diag_run_'))
print(f'Test run_dir: {run_dir}')

state = OptimizerState(
    control=ControlState(run_dir=run_dir, input_dcp=Path('/tmp/test.dcp')),
    timing=TimingState(),
    context=ContextState(),
    model=ModelState(current_model='test'),
    iteration=IterationState(current=0),
)
state.timing.critical_paths = [
    CriticalPathEntry(
        cells=['cell_a', 'cell_b', 'cell_c'],
        path_length=3,
        iteration=0,
        slack=-1.234,
        logic_delay=2.0,
        net_delay=0.5,
        levels=3,
        nodes=[
            PathNode(kind='cell', name='cell_a', cell_type='LUT6', location='SLICE_X1Y1', incr_delay=0.5),
            PathNode(kind='net', name='net_ab', fanout=2, incr_delay=0.2, net_status='routed'),
            PathNode(kind='cell', name='cell_b', cell_type='CARRY8', location='SLICE_X1Y1', incr_delay=0.8),
        ],
        startpoint='cell_a/C',
        endpoint_pin='cell_c/D',
        arrival_time=4.5,
        required_time=3.266,
        top_delay_nodes=[
            PathNode(kind='cell', name='cell_b', cell_type='CARRY8', incr_delay=0.8),
        ],
        clock=ClockDomainInfo(
            source_clock='clk', dest_clock='clk',
            path_group='clk', path_type='Setup',
            requirement=4.0, clock_skew=0.0, clock_uncertainty=0.05,
        ),
    )
]
state.timing.high_fanout_nets = [
    {'net_name': 'reset', 'fanout': 500, 'driver_cell': 'reset_buf', 'load_types': {'FDRE': 500}},
]
state.timing.congestion_data = {'global_score': 0.05}
state.timing.design_info = {
    'cell_count': 10000,
    'net_count': 12000,
    'top_module': 'top',
    'top_cell_types': {'LUT6': 3000, 'FDRE': 4000, 'CARRY8': 500},
    'part': 'xcu250-figd2104-2L-e',
}
state.timing.field_freshness = {
    'critical_path_cells': 'fresh',
    'high_fanout_nets': 'fresh',
    'congestion_data': 'fresh',
    'route_status': 'fresh',
    'design_info': 'fresh',
}
state.timing.failing_endpoint_names = ['cell_c']

space = build_state_space(state)

design_data_path = None
phase = LoopPhase.ANALYZE
full_critical_paths = []

print(f'run_dir = {run_dir}')

try:
    ddm = DesignDataManager(run_dir)
    print(f'DesignDataManager base_dir = {ddm._base_dir}')
    
    if state.timing.critical_paths:
        full_critical_paths = list(state.timing.critical_paths)
        print(f'critical_paths count: {len(full_critical_paths)}')
    
    hf_raw = state.timing.high_fanout_nets
    if hf_raw is not None:
        print(f'high_fanout_nets count: {len(hf_raw)}')
    
    congestion_raw = state.timing.congestion_data
    if isinstance(congestion_raw, dict):
        print(f'congestion_data keys: {list(congestion_raw.keys())}')
    
    if state.timing.design_info is not None:
        print(f'design_info keys: {list(state.timing.design_info.keys())}')
    
    current_iter = state.iteration.current
    print(f'current_iter = {current_iter}, last = {state.context.design_data.last_snapshot_iteration}')
    
    if current_iter != state.context.design_data.last_snapshot_iteration:
        print('Calling store_snapshot...')
        iter_dir = ddm.store_snapshot(
            critical_paths=full_critical_paths,
            high_fanout_nets=state.timing.high_fanout_nets,
            congestion_data=state.timing.congestion_data,
            route_status=state.timing.route_status,
            design_info=state.timing.design_info,
            failing_endpoint_names=state.timing.failing_endpoint_names,
            field_freshness=state.timing.field_freshness,
            iteration=current_iter,
            phase=phase.value if hasattr(phase, 'value') else str(phase),
        )
        design_data_path = iter_dir
        state.context.design_data.last_snapshot_iteration = current_iter
        print(f'store_snapshot OK: {iter_dir}')
except Exception as e:
    print(f'EXCEPTION: {type(e).__name__}: {e}')
    traceback.print_exc()

print(f'design_data_path = {design_data_path}')

try:
    exclusion_sets = {k: set() for k in ['critical_path_cells','critical_path_full','high_fanout_nets','congestion_data','route_status','design_info']}
    snap = format_state_space_for_llm(space, phase=phase, design_data_path=design_data_path, exclusion_sets=exclusion_sets)
    print(f'format_state_space_for_llm OK, len={len(snap)}')
except Exception as e:
    print(f'format_state_space_for_llm EXCEPTION: {type(e).__name__}: {e}')
    traceback.print_exc()

dd = run_dir / 'design_data'
print(f'design_data dir exists: {dd.exists()}')
if dd.exists():
    for p in sorted(dd.rglob('*')):
        print(f'  {p.relative_to(run_dir)}')
