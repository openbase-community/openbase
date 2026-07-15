`openbase-coder` is the main Openbase Coder runtime repository.

The CLI owns the Openbase routines product domain: definitions, persisted
state, schedule evaluation, polling, command execution, run history, and
health reporting. Routine execution may call generic Super Agents thread,
turn, queue, and session primitives, but routine models and scheduler behavior
must remain in this repository rather than `super-agents`.
