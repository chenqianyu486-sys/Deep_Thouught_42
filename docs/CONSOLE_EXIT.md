# Console Exit Intervention

A lightweight mechanism allows users to gracefully terminate the optimizer from the console while it is running, without needing to kill the process.

## How to Use

During optimization, simply type `quit` in the terminal and press Enter:

```bash
$ python dcp_optimizer.py input.dcp --output output.dcp
FPGA Design Optimization Agent
================================
...
=== Starting LLM-Driven Optimization ===

*** Iteration 3 ***
[Agent] Looking at timing report...

# In another terminal window, or while the process is running in the background:
$ quit    <-- type this and press Enter to request graceful exit
```

## Behavior

When `quit` is entered:

1. **During an iteration** (between tool rounds): The current tool round completes, then the loop breaks and the optimizer saves a checkpoint and prints a summary before exiting.

2. **At iteration start**: The iteration is aborted before any LLM call is made, a checkpoint is saved, and the optimizer exits gracefully.

The optimizer will print a message like:
```
User requested exit via console 'quit' command
User requested exit at iteration 3, saving checkpoint and exiting gracefully...
```

## Exit Points

The exit check occurs at two places:

| Location | Trigger | What Happens |
|----------|---------|--------------|
| `optimize()` while loop | Before starting a new iteration | Iteration skipped, checkpoint saved, summary printed |
| `get_completion()` tool_round loop | Between tool rounds | Current round completes, then exits with checkpoint |

## Implementation

- Uses a `threading.Event` (`_user_exit_requested`) as a signal flag
- A daemon thread (`_start_console_reader()`) listens on `stdin` for the line `quit`
- The main thread checks the flag at controlled interruption points (iteration start, tool round boundaries)
- No signal handlers (`SIGINT`, `SIGTERM`) are used — this avoids interference with subprocess management in the MCP servers
