# Role: chain

This role is run by chain through `ChainExecutor`, not by an agent that reads a prompt, so nothing reads this file. It exists because the role vocabulary is the set of files in this directory, and `build` and `bug` tasks route to `chain`.
