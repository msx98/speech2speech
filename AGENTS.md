# Running on rocbox

Run the server inside rocbox, which has GPU access. From the desktop:

```sh
ssh -t rocbox bash /home/ubuntu/sound-server/src/run.sh
```

Inside rocbox, use `bash ./src/run.sh` from the repository root. The shared
checkout has rejected direct script execution with `Text file busy`; invoking
the script through Bash works around this.

The runner uses the repository's `.venv`, loads `.env` and `studio.env`, adapts
container paths, and starts one Uvicorn worker on `0.0.0.0:8000` by default.
On a read-only checkout, it seeds voices and sessions into
`${XDG_DATA_HOME:-$HOME/.local/share}/sound-server` and saves changes there.

For runner changes, successful startup and HTTP checks of `/health`,
`/studio/health`, `/v1/models`, and `/` are sufficient validation. Stop once
these pass. Check optional backends or run inference only when the task
requires it or a failure points there. Stop any temporary validation server
before finishing.
