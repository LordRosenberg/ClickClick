# AndroidWorld benchmark adapter

`agent.integrations.android_world.ClickClickAgent` is an optional, benchmark-only
adapter. AndroidWorld remains responsible for task setup, its external step
budget, scripted success evaluation, teardown, and checkpoints. ClickClick runs
the reasoning loop and performs actions on the same emulator.

The adapter runs the current revisable-plan runtime, defaulting to Planner +
Executor with optional Reviewer. Each AndroidWorld `agent.step(goal)` call gives
`run_task` an incremental budget of one submitted agent action. Non-action
stage/replan/review decisions do not consume that action budget. AndroidWorld's
`episode_length` counts calls to `agent.step`, not LLM requests; a final non-action
termination call can still count as an episode step. `clickclick_role_invocations`
is the cumulative provider-request count; individual roles remain in the traces.

The [full-suite evaluation report](androidworld-results-20260921.md) describes
the protocol behind 115/116. That evaluation executes one persistent `run_task`
per episode and has different outer step accounting from this step-oriented adapter.

## Register the adapter

Install both repositories in the same Python environment, then add this import
to the AndroidWorld checkout's `run.py`:

```python
from agent.integrations.android_world import ClickClickAgent
```

Add a branch to AndroidWorld's `_get_agent` function:

```python
elif _AGENT_NAME.value == "clickclick":
  agent = ClickClickAgent(
      env,
      device_serial=f"emulator-{_DEVICE_CONSOLE_PORT.value}",
  )
```

Configure ClickClick's model environment variables as usual. Do not enable the
fixture driver. The AndroidWorld emulator and ClickClick must use the same ADB
serial.

Run a smoke task first:

```bash
python run.py \
  --suite_family=android_world \
  --agent_name=clickclick \
  --tasks=ContactsAddContact \
  --perform_emulator_setup
```

`--perform_emulator_setup` is only for the first AndroidWorld setup. Remove it
from later runs. To override the default serial (`emulator-5554`), pass
`device_serial` in the constructor or set
`CLICKCLICK_ANDROIDWORLD_DEVICE_SERIAL`.

## Result interpretation

Each AndroidWorld checkpoint step contains a compact ClickClick payload:

- `clickclick_task_id`: link to ClickClick's local database and traces.
- `clickclick_status`: ClickClick's internal task status.
- `clickclick_step_number`: persisted Executor-decision count, including
  non-action decisions.
- `clickclick_device_action_count`: cumulative submitted agent-action count.
  A Harness-owned compound primitive may issue multiple audited ADB subactions,
  but consumes one AndroidWorld step because it is one `agent.step` action.
- `clickclick_role_invocations`: cumulative model-provider request count.
- `clickclick_executor_steps`: all action/boundary decisions produced during
  this AndroidWorld call.

ClickClick reporting success only ends its agent episode. AndroidWorld's own
`task.is_successful(env)` remains the benchmark score authority.
