# Openbase

Openbase scaffolds Django+React projects that are:
1. Easily-deployable
2. Vibe-coding compatible
3. Support registration, login, security, and payment out of the box.
4. Support hosting an arbitrary number of projects on a single server
5. Split among repos so that you don't have to share your whole project at once (using [multi](https://multi.bighelp.ai/))

Not only that, Openbase comes with a backend visualizer, so that you can see what is going on in with the backend development **without having to look at the code**.

## Getting started with scaffolding
To get started scaffolding a new project, follow the steps below:

- Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if you haven't already
- Install Openbase: `uv tool install openbase`
- Install the [multi](https://multi.bighelp.ai/) CLI tool with `uv tool install multi-workspace`
- Install the [boilersync](https://github.com/montaguegabe/boilersync-cli) CLI tool with `uv tool install boilersync`
- Make a new directory named after the project you want to create (e.g. `my-project`) and `cd` into it
- Run `openbase init` to scaffold your project.
- Make sure you have Docker Desktop running. Run `./scripts/setup.sh` to setup the initial database and python virtual env for your project.
- Open the project in Cursor or VS Code.
- Select the debug runnable with the name of your project and press the run button.  Your project starter will be live at `localhost` (port 80).

## AI code generation

While the above steps produce a Django/React empty project, you can take it a step further by bootstrapping your idea with AI, running frontend and backend development in parallel with Claude Code.  To do this:

- Make sure you have installed Claude Code.
- Create ./.openbase/DESCRIPTION.md with a detailed description of what you want your web app to be.
- WARNING: The following step calls Claude Code with `--dangerously-skip-permissions` inside your project directory.  There is always a risk with --dangerously-skip-permissions that Claude Code will run harmful commands.  If you are concerned, you can always continue vibe coding from the scaffolded project without calling the subsequent step, or run within a VS Code/Cursor Dev Container.
- Run `openbase generate` to bootstrap the new idea.  This will parallelize development across frontend and backend, and will run with `--dangerously-skip-permissions` without checking permissions with the user.  Be patient while it generates.

## Backend Visualizer
- Run `openbase server` then go to localhost:8001 to see the Openbase Console

## Deployment
- Create a Heroku account and install the Heroku CLI.
- Run `scripts/new_deployment my-heroku-server-name existing-app-to-copy` to create a new Heroku app with mini Postgres and Redis attachments.  Env variables will be copied from `existing-app-to-copy`.
- Run `scripts/gha_build my-heroku-server-name ghp_XYZ <app package list>` to build Docker images for deployment, where ghp_XYZ is a Github access token with clone access to the backend/API repos you want to deploy, and `<app package list>` is a list of the backend/API repoos.  Here is where you decide which projects you want to live on the server. For example to deploy `project1-api`, `project2-api`, and `project3-api` on server `my-heroku-server-name`:
```
./scripts/gha_build my-heroku-server-name ghp_XYZ \
"git+https://github.com/my-github/project1-api.git@main
git+https://github.com/my-github/project2-api.git@main
git+https://github.com/my-github/project3-api.git@main
./scripts/gha_deploy my-heroku-server-name
```
-   `scripts/gha_deploy my-heroku-server-name` to deploy your project to Heroku.
- Make sure your domain is in `ALLOWED_HOSTS` environment variable.
- Run `heroku run bash -a my-heroku-server-name` and via the `manage.py shell` command create a new `Site` object with your host name.
- Log into Django Admin and finish filling out your site's details.
- As of now, you will need to deploy the frontend React app to an S3 bucket yourself, and specify the bucket name via env variable and prefix name via Django admin. Automation of this is coming soon.
