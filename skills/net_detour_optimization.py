# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Net Detour Analysis and Cell Placement Optimization Skill

This module provides pure functions for:
- Analyzing detour ratios of nets on critical paths
- Optimizing cell placement based on centroid of connections

The skill layer contains pure functions; MCP tool wrappers delegate to these.
"""

from dataclasses import dataclass, field
from typing import Optional

from skills.base import Skill, SkillMetadata, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill


@dataclass
class DetourAnalysisResult:
    """Result of detour analysis for a single cell."""
    cell_name: str
    in_pin: Optional[str]
    out_pin: Optional[str]
    max_detour_ratio: float
    in_net_detour: Optional[float] = None
    out_net_detour: Optional[float] = None
    source_pin: Optional[str] = None       # Pin that triggered max detour
    worst_sink_pin: Optional[str] = None  # Specific sink pin that triggered max detour


@dataclass
class PlacementOptimizationResult:
    """Result of cell placement optimization."""
    cell_name: str
    original_site: Optional[str]
    new_site: Optional[str]
    centroid_tile: Optional[str]
    nets_unrouted: list[str] = field(default_factory=list)
    status: str = "pending"  # "success", "error", "skipped"
    message: Optional[str] = None


def _group_pins_by_cell(pin_paths: list[str]) -> list[tuple[Optional[str], Optional[str], str]]:
    """
    Group consecutive pins by cell and identify data path pins.

    pin_paths format: ["src_ff/Q", "lut1/I2", "lut1/O", "lut2/I0", "lut2/O", "dst_ff/D"]

    Boundary conditions:
    - 首尾FF: First pin is FF output (src_ff/Q), last pin is FF input (dst_ff/D)
    - 跨cell跳转: When adjacent pins belong to different cells, it indicates a net crossing
    - 处理逻辑: Consecutive pins from the same cell form a group (e.g., lut1/I2, lut1/O)

    Args:
        pin_paths: List of hierarchical pin names from critical path

    Returns:
        List of (in_pin, out_pin, cell_name) tuples representing data paths.
        in_pin is the input pin of the cell, out_pin is the output pin.
        Returns (None, out_pin, cell_name) for source FF, (in_pin, None, cell_name) for sink FF.
    """
    from typing import Tuple, List, Dict

    if not pin_paths or len(pin_paths) < 2:
        return []

    # Parse pin names to extract cell name and pin name
    # Format: "cell_name/pin_name" or "cell_name/subcell/pin_name"
    def parse_pin(pin_path: str) -> Tuple[str, str]:
        parts = pin_path.split('/')
        if len(parts) < 2:
            return pin_path, pin_path
        cell_name = parts[0]
        pin_name = '/'.join(parts[1:])
        return cell_name, pin_name

    # Check if pin is an output (ends with O or Q)
    def is_output_pin(pin_name: str) -> bool:
        return pin_name.endswith('O') or pin_name.endswith('Q')

    # Check if pin is an input (starts with I or is D)
    def is_input_pin(pin_name: str) -> bool:
        return pin_name.startswith('I') or pin_name == 'D'

    # Parse all pins
    parsed_pins = [parse_pin(p) for p in pin_paths]

    # Track in_pin and out_pin for each cell
    cell_pins: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

    i = 0
    while i < len(parsed_pins) - 1:
        curr_cell, curr_pin = parsed_pins[i]
        next_cell, next_pin = parsed_pins[i + 1]

        if curr_cell == next_cell:
            # Same cell: both pins belong to this cell
            # First determine which is input and which is output
            if curr_cell not in cell_pins:
                if is_input_pin(curr_pin) and is_output_pin(next_pin):
                    cell_pins[curr_cell] = (curr_pin, next_pin)
                elif is_output_pin(curr_pin) and is_input_pin(next_pin):
                    cell_pins[curr_cell] = (next_pin, curr_pin)
                # else: same-cell but not input-output pair, skip
            else:
                # Cell already has pins, might need to update missing ones
                old_in, old_out = cell_pins[curr_cell]
                if is_input_pin(curr_pin) and old_in is None:
                    old_in = curr_pin
                elif is_input_pin(next_pin) and old_in is None:
                    old_in = next_pin
                if is_output_pin(curr_pin) and old_out is None:
                    old_out = curr_pin
                elif is_output_pin(next_pin) and old_out is None:
                    old_out = next_pin
                cell_pins[curr_cell] = (old_in, old_out)
        else:
            # Different cells: net crossing
            # curr is output of curr_cell, next is input of next_cell
            if is_output_pin(curr_pin):
                if curr_cell not in cell_pins:
                    cell_pins[curr_cell] = (None, curr_pin)
                else:
                    # Update out_pin if already exists
                    old_in, _ = cell_pins[curr_cell]
                    cell_pins[curr_cell] = (old_in, curr_pin)
            if is_input_pin(next_pin):
                if next_cell not in cell_pins:
                    cell_pins[next_cell] = (next_pin, None)
                else:
                    # Update in_pin if already exists
                    _, old_out = cell_pins[next_cell]
                    cell_pins[next_cell] = (next_pin, old_out)
        i += 1

    # Handle the last pin if it's an output (might be sink FF)
    if parsed_pins:
        last_cell, last_pin = parsed_pins[-1]
        if is_output_pin(last_pin):
            if last_cell not in cell_pins:
                cell_pins[last_cell] = (None, last_pin)
            else:
                old_in, _ = cell_pins[last_cell]
                cell_pins[last_cell] = (old_in, last_pin)

    # Convert to result list
    result = [(in_pin, out_pin, cell) for cell, (in_pin, out_pin) in cell_pins.items()]

    # Sort by first occurrence in pin_paths
    def first_occurrence(cell_name: str) -> int:
        for i, (c, _) in enumerate(parsed_pins):
            if c == cell_name:
                return i
        return len(parsed_pins)

    result.sort(key=lambda x: first_occurrence(x[2]))

    return result


def _compute_routed_path_length(net, sink_pin) -> int:
    """
    Compute the routed path length from source to sink by walking through PIPs.

    Walks backward through PIPs from sink pin to source, summing tile-to-tile
    Manhattan distances at each hop.

    Args:
        net: RapidWright Net object with PIPs
        sink_pin: SitePinInst of the sink pin

    Returns:
        Total path length as int (sum of tile Manhattan distances)
    """
    try:
        from com.xilinx.rapidwright.router import RouteNode
        from com.xilinx.rapidwright.device import Tile
    except ImportError:
        return 0

    # Build node map from PIPs: end_node -> start_node
    pips = net.getPIPs()
    if not pips or pips.size() == 0:
        return 0

    node_map = {}
    for pip in pips:
        end_node = pip.getEndNode()
        start_node = pip.getStartNode()
        node_map[end_node] = start_node

    # Get sink node
    sink_node = sink_pin.getConnectedNode()
    if sink_node is None:
        return 0

    # Walk backward from sink to source
    total_length = 0
    current_node = sink_node
    visited = set()

    while current_node in node_map:
        if current_node in visited:
            break  # Avoid cycles
        visited.add(current_node)

        prev_node = node_map[current_node]

        # Get tile coordinates for Manhattan distance
        try:
            current_tile = current_node.getTile()
            prev_tile = prev_node.getTile()

            if current_tile and prev_tile:
                # Manhattan distance = |col1-col2| + |row1-row2|
                col_dist = abs(current_tile.getColumn() - prev_tile.getColumn())
                row_dist = abs(current_tile.getRow() - prev_tile.getRow())
                total_length += col_dist + row_dist
        except Exception:
            pass

        current_node = prev_node

    return total_length


def _detour_ratio(net, sink_pin) -> tuple[float, Optional[str], Optional[str]]:
    """
    Calculate detour ratio for a net's sink pin.

    detour_ratio = routed_path_length / manhattan_distance(source_tile, sink_tile)

    Args:
        net: RapidWright Net object
        sink_pin: SitePinInst of the sink pin

    Returns:
        Tuple of (detour_ratio, source_pin_name, worst_sink_pin_name)
    """
    try:
        from com.xilinx.rapidwright.device import Tile
    except ImportError:
        return 0.0, None, None

    # Get source and sink tiles
    source_pins = net.getSourcePins()
    if not source_pins or source_pins.size() == 0:
        return 0.0, None, None

    source_pin = source_pins[0]
    source_tile = source_pin.getTile()
    sink_tile = sink_pin.getTile()

    if source_tile is None or sink_tile is None:
        return 0.0, None, None

    # Calculate Manhattan distance
    manhattan_dist = abs(source_tile.getColumn() - sink_tile.getColumn()) + \
                     abs(source_tile.getRow() - sink_tile.getRow())

    if manhattan_dist == 0:
        return 0.0, None, None

    # Calculate routed path length
    routed_length = _compute_routed_path_length(net, sink_pin)

    source_pin_name = str(source_pin.getName()) if source_pin else None
    sink_pin_name = str(sink_pin.getName()) if sink_pin else None

    detour_ratio = routed_length / manhattan_dist
    return detour_ratio, source_pin_name, sink_pin_name


def analyze_net_detour(design, pin_paths: list[str], detour_threshold: float = 2.0) -> dict[str, DetourAnalysisResult]:
    """
    Analyze detour ratios for cells on critical paths.

    For each interior cell on the critical path, examines both the incoming net
    (feeding the cell) and the outgoing net (driven by it) to compute the worst-case
    detour ratio. A high detour ratio may indicate the cell is poorly placed.

    Args:
        design: RapidWright Design object (must be loaded)
        pin_paths: List of pin names from Vivado's extract_critical_path_pins
                  Format: ["src_ff/Q", "lut1/I2", "lut1/O", "lut2/I0", "lut2/O", "dst_ff/D"]
        detour_threshold: Minimum ratio to flag as problematic (default 2.0)

    Returns:
        Dict keyed by cell_name containing DetourAnalysisResult for cells
        with max_detour_ratio > threshold, sorted descending by ratio.
    """
    if design is None:
        return {}

    # Group pins by cell to identify data paths
    cell_groups = _group_pins_by_cell(pin_paths)

    results = {}

    for in_pin, out_pin, cell_name in cell_groups:
        cell = design.getCell(cell_name)
        if cell is None:
            continue

        max_ratio = 0.0
        source_pin_result = None
        worst_sink_result = None
        in_net_detour = None
        out_net_detour = None

        # Analyze incoming net (input pin)
        if in_pin:
            # Find the net connected to this input pin
            try:
                hier_port = design.getNetlist().getHierPortInstFromName(f"{cell_name}/{in_pin}")
                if hier_port:
                    net = hier_port.getNet()
                    if net:
                        # Get all source pins of this net
                        source_pins = net.getSourcePins()
                        if source_pins and source_pins.size() > 0:
                            src_pin = source_pins[0]
                            ratio, src_pin_name, sink_pin_name = _detour_ratio(net, src_pin)
                            if ratio > max_ratio:
                                max_ratio = ratio
                                source_pin_result = src_pin_name
                                worst_sink_result = sink_pin_name
                            in_net_detour = ratio
            except Exception:
                pass

        # Analyze outgoing net (output pin) - check max across all sinks
        if out_pin:
            try:
                hier_port = design.getNetlist().getHierPortInstFromName(f"{cell_name}/{out_pin}")
                if hier_port:
                    net = hier_port.getNet()
                    if net:
                        sink_pins = net.getSinkPins()
                        if sink_pins and sink_pins.size() > 0:
                            # For output pin, check max detour across all sinks
                            # Since SitePinInst is the net's source, checking it against
                            # itself would yield zero, so we iterate over sinks
                            for sink_p in sink_pins:
                                ratio, src_pin_name, sink_pin_name = _detour_ratio(net, sink_p)
                                if ratio > max_ratio:
                                    max_ratio = ratio
                                    source_pin_result = src_pin_name
                                    worst_sink_result = sink_pin_name
                            out_net_detour = max((_detour_ratio(net, sp)[0] for sp in sink_pins), default=0.0)
            except Exception:
                pass

        # Filter by threshold and store result
        if max_ratio > detour_threshold:
            results[cell_name] = DetourAnalysisResult(
                cell_name=cell_name,
                in_pin=in_pin,
                out_pin=out_pin,
                max_detour_ratio=max_ratio,
                in_net_detour=in_net_detour,
                out_net_detour=out_net_detour,
                source_pin=source_pin_result,
                worst_sink_pin=worst_sink_result
            )

    # Sort by max_detour_ratio descending
    sorted_results = dict(sorted(results.items(), key=lambda x: x[1].max_detour_ratio, reverse=True))

    return sorted_results


def _get_cell_physical_nets(cell) -> list:
    """
    Get all physical nets connected to a cell for unrouting.

    Args:
        cell: RapidWright Cell object

    Returns:
        List of Net objects connected to the cell
    """
    nets = []

    try:
        # Get SitePinInsts from the cell's site
        site_pins = cell.getSitePinInsts()
        if site_pins:
            for pin in site_pins:
                try:
                    net = pin.getNet()
                    if net and net not in nets:
                        nets.append(net)
                except Exception:
                    pass
    except Exception:
        pass

    return nets


def optimize_cell_placement(
    design,
    cell_names: list[str]
) -> dict[str, PlacementOptimizationResult]:
    """
    Optimize cell placement by moving cells to centroid of their connections.

    For each cell:
    1. Collect tile locations of all connected pins
    2. Compute centroid using ECOPlacementHelper.getCentroidOfPoints()
    3. Unplace cell and unroute its nets
    4. Spiral outward from centroid to find an empty compatible site
    5. Place cell at new site and re-route intra-site wiring

    Args:
        design: RapidWright Design object (must be loaded and placed)
        cell_names: List of cell names to optimize

    Returns:
        Dict keyed by cell_name containing PlacementOptimizationResult
    """
    if design is None:
        return {}

    try:
        from com.xilinx.rapidwright.eco import ECOPlacementHelper, PlacementModification
        from com.xilinx.rapidwright.design.tools import DesignTools
    except ImportError:
        # Return error for all cells
        return {name: PlacementOptimizationResult(
            cell_name=name,
            original_site=None,
            new_site=None,
            centroid_tile=None,
            status="error",
            message="RapidWright ECO classes not available"
        ) for name in cell_names}

    results = {}

    for cell_name in cell_names:
        cell = design.getCell(cell_name)

        if cell is None:
            results[cell_name] = PlacementOptimizationResult(
                cell_name=cell_name,
                original_site=None,
                new_site=None,
                centroid_tile=None,
                status="error",
                message=f"Cell '{cell_name}' not found in design"
            )
            continue

        if not cell.isPlaced():
            results[cell_name] = PlacementOptimizationResult(
                cell_name=cell_name,
                original_site=None,
                new_site=None,
                centroid_tile=None,
                status="skipped",
                message="Cell is not placed, skipping"
            )
            continue

        original_site = str(cell.getSite().getName())

        # Step 1: Collect tile locations of all connected pins
        connected_tiles = []
        try:
            nets = _get_cell_physical_nets(cell)
            for net in nets:
                # Source pins
                for src_pin in net.getSourcePins():
                    tile = src_pin.getTile()
                    if tile:
                        connected_tiles.append((tile.getColumn(), tile.getRow()))

                # Sink pins
                for sink_pin in net.getSinkPins():
                    tile = sink_pin.getTile()
                    if tile:
                        connected_tiles.append((tile.getColumn(), tile.getRow()))
        except Exception as e:
            results[cell_name] = PlacementOptimizationResult(
                cell_name=cell_name,
                original_site=original_site,
                new_site=None,
                centroid_tile=None,
                status="error",
                message=f"Failed to collect connected tiles: {str(e)}"
            )
            continue

        if not connected_tiles:
            results[cell_name] = PlacementOptimizationResult(
                cell_name=cell_name,
                original_site=original_site,
                new_site=None,
                centroid_tile=None,
                status="skipped",
                message="No connected tiles found"
            )
            continue

        # Step 2: Compute centroid
        centroid_col = sum(t[0] for t in connected_tiles) // len(connected_tiles)
        centroid_row = sum(t[1] for t in connected_tiles) // len(connected_tiles)

        try:
            device = design.getDevice()
            # Get site at centroid location
            centroid_site = ECOPlacementHelper.getSiteAtLocation(device, cell.getSiteTypeEnum(),
                                                                  centroid_col, centroid_row)
            if centroid_site is None:
                # Try to find any compatible site near centroid
                centroid_site = ECOPlacementHelper.getNearestCompatibleEmptySite(
                    device, cell.getSiteTypeEnum(), centroid_col, centroid_row)
        except Exception:
            centroid_site = None

        # Step 3: Unplace cell and unroute connected nets
        nets_unrouted = []
        try:
            nets = _get_cell_physical_nets(cell)
            for net in nets:
                net_name = str(net.getName())
                net.unroute()
                nets_unrouted.append(net_name)

            DesignTools.fullyUnplaceCell(cell)
        except Exception as e:
            results[cell_name] = PlacementOptimizationResult(
                cell_name=cell_name,
                original_site=original_site,
                new_site=None,
                centroid_tile=None,
                nets_unrouted=nets_unrouted,
                status="error",
                message=f"Failed to unplace/unroute: {str(e)}"
            )
            continue

        # Step 4: Spiral search for empty site
        new_site = None
        try:
            if centroid_site and not centroid_site.isOccupied():
                new_site = centroid_site
            else:
                # Spiral outward from centroid
                new_site = ECOPlacementHelper.spiralOutFrom(
                    device, cell.getSiteTypeEnum(), centroid_col, centroid_row)

                # Check if site is empty
                if new_site and new_site.isOccupied():
                    # Keep spiraling
                    max_spiral_steps = 20
                    for step in range(max_spiral_steps):
                        new_site = ECOPlacementHelper.spiralOutFrom(
                            device, cell.getSiteTypeEnum(),
                            new_site.getInstanceX(), new_site.getInstanceY())
                        if new_site and not new_site.isOccupied():
                            break
                    else:
                        new_site = None
        except Exception:
            new_site = None

        if new_site is None:
            results[cell_name] = PlacementOptimizationResult(
                cell_name=cell_name,
                original_site=original_site,
                new_site=None,
                centroid_tile=f"col={centroid_col}, row={centroid_row}",
                nets_unrouted=nets_unrouted,
                status="error",
                message="Could not find empty compatible site"
            )
            continue

        # Step 5: Place cell at new site and route intra-site
        try:
            design.placeCell(cell, new_site)

            # Route intra-site wiring
            site_inst = new_site.getSiteInstance()
            if site_inst:
                site_inst.routeSite()

            new_site_name = str(new_site.getName())

            results[cell_name] = PlacementOptimizationResult(
                cell_name=cell_name,
                original_site=original_site,
                new_site=new_site_name,
                centroid_tile=f"col={centroid_col}, row={centroid_row}",
                nets_unrouted=nets_unrouted,
                status="success",
                message=f"Moved from {original_site} to {new_site_name}"
            )

        except Exception as e:
            results[cell_name] = PlacementOptimizationResult(
                cell_name=cell_name,
                original_site=original_site,
                new_site=None,
                centroid_tile=f"col={centroid_col}, row={centroid_row}",
                nets_unrouted=nets_unrouted,
                status="error",
                message=f"Failed to place at new site: {str(e)}"
            )

    return results


@skill(
    name="analyze_net_detour",
    description="Analyze detour ratios for cells on critical paths. "
                "detour_ratio = routed_path_length / manhattan_distance. "
                "Ratio > 2.0 suggests cell may benefit from re-placement.",
    category=SkillCategory.ANALYSIS,
    parameters=[
        ParameterSpec("pin_paths", list, "Pin path list from Vivado's extract_critical_path_pins"),
        ParameterSpec("detour_threshold", float, "Minimum ratio to flag as problematic", default=2.0)
    ],
    required_context=["design"]
)
class AnalyzeNetDetourSkill(Skill):
    """Skill for analyzing detour ratios of nets on critical paths."""

    def get_metadata(self) -> SkillMetadata:
        return self._skill_metadata

    def execute(self, context: SkillContext, pin_paths: list, detour_threshold: float = 2.0) -> SkillResult:
        try:
            results = analyze_net_detour(context.design, pin_paths, detour_threshold)
            return SkillResult(success=True, data=results)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        if "pin_paths" not in kwargs:
            return False, "pin_paths is required"
        pin_paths = kwargs["pin_paths"]
        if not isinstance(pin_paths, list) or len(pin_paths) < 2:
            return False, "pin_paths must be a list with at least 2 elements"
        return True, ""


@skill(
    name="optimize_cell_placement",
    description="Move cells to centroid of their connections for better placement. "
                "Uses spiral search to find empty compatible sites.",
    category=SkillCategory.PLACEMENT,
    parameters=[
        ParameterSpec("cell_names", list, "List of cell names to optimize")
    ],
    required_context=["design"]
)
class OptimizeCellPlacementSkill(Skill):
    """Skill for optimizing cell placement based on centroid of connections."""

    def get_metadata(self) -> SkillMetadata:
        return self._skill_metadata

    def execute(self, context: SkillContext, cell_names: list) -> SkillResult:
        try:
            results = optimize_cell_placement(context.design, cell_names)
            return SkillResult(success=True, data=results)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        if "cell_names" not in kwargs:
            return False, "cell_names is required"
        if not isinstance(kwargs["cell_names"], list):
            return False, "cell_names must be a list"
        return True, ""