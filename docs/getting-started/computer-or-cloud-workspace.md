# Computer or Cloud Workspace?

Openbase Coder runs a private coding backend somewhere you control, and the phone apps connect to it. When you sign in on a phone for the first time and your account has no backend yet, the app asks where that backend should live:

- **Set Up a New Computer** — install Openbase Coder on a Mac (or Linux machine) you own and pair the phone with it.
- **Use a Cloud Workspace** — let Openbase Cloud host a small private workspace for you, so you can start from the phone with no computer at all.

The app only asks when it has to. If your account already has a computer running Openbase Coder or a Cloud Workspace, the phone skips this choice and goes straight to pairing. This page helps you pick when you are asked.

## Prefer an always-on computer

If you have a choice, set up an always-on computer first, such as a Mac mini or another desktop that stays powered on and online. A backend that never sleeps is what makes Openbase Coder feel like a colleague: you can call it from anywhere, agents keep working while your laptop is closed, and long tasks finish overnight. A laptop works too, but every call and every agent run depends on the laptop being awake and connected.

Follow [Getting Started](index.md) for the install itself. The [Mac app](mac-app.md) is the no-terminal path; the [developer setup](developer-setup.md) keeps everything inspectable.

## The first choice is not final

Computers can be set up and added later, so nothing here locks you in:

- A phone that started with a Cloud Workspace can pair with a computer later. Install Openbase Coder on the computer and sign in to the same Openbase account; the phone lists it under **Settings → Backend Host** next to the workspace, and you switch between them with a tap.
- A phone that started with a computer can add a Cloud Workspace or a larger [Cloud DevSpace](../cloud-devspace.md) later, and can add more computers (a laptop and a Mac mini, for example) as they are set up.
- [Sync between your computers](../code-sync.md) keeps the same projects on every backend you add.

## What a Cloud Workspace gives you

A Cloud Workspace is a small private Linux container that Openbase Cloud starts for your account. It runs the full Openbase Coder runtime, pairs itself with your phone automatically, and goes to sleep when idle so it costs nothing while you are not using it. The phone wakes it when you call.

It is sized for talking to agents and working on ordinary projects, not for heavy builds. Today a Cloud Workspace has:

| Resource | Cloud Workspace |
| --- | --- |
| Memory | 4 GB |
| CPU | 1 virtual core |
| Disk | 5 GB |

In practice that means:

- **Fine:** editing and running web apps, Python and Node projects, scripts, documentation, small services, and anything an agent can do from a terminal.
- **Tight:** large dependency trees. A few projects with `node_modules` or a Python virtual environment each fit in 5 GB; keeping many checked out at once does not.
- **Not a fit:** large compiles (big Rust, C++, or JVM builds), building container images, machine-learning workloads, and anything that needs more than 4 GB of memory. iOS, macOS, and Android app builds need a Mac or a full desktop in any case.

If your projects look like the last two, start with a computer, or launch a larger [Cloud DevSpace](../cloud-devspace.md), which is a full cloud machine with sizes you pick.

The exact figures can change as the service evolves; the workspace screen in the app shows the current allowance for your account.

### Free trial hours

New accounts get a free trial allowance for Cloud Workspace time: about 80 hours a month, renewed monthly for the first four months. Time only counts while the workspace is awake, and it sleeps on its own after about an hour without a call or an agent working, so a trial covers a lot of normal use. When the allowance runs out, the workspace pauses until you subscribe; your projects stay where they are.

## Pairing after you choose

Both paths end in the same place: the phone and the backend join a private network (Openbase VPN or Openbase Direct) and the app finishes pairing on its own. See the [iOS app](../ios-tabs.md#onboarding) and [Android app](../downloads.md) pages for what the phone shows along the way, and [Troubleshooting](../troubleshooting.md) if pairing stalls.
