# OpenBench

OpenBench is an open-source Chess Engine Testing Framework for UCI engines. OpenBench provides a lightweight interface and client to facilitate running fixed-game tests as well as SPRT tests to benchmark changes to engines for performance and stability. OpenBench supports [Fischer Random Chess](https://en.wikipedia.org/wiki/Chess960).

OpenBench is the primary testing framework used for the development of [Ethereal.](https://github.com/AndyGrant/Ethereal) The primary instance of OpenBench can be found at [http://chess.grantnet.us](http://chess.grantnet.us/). The Primary instance of OpenBench supports development for
[Berserk](https://github.com/jhonnold/berserk), [Bit-Genie](https://github.com/Aryan1508/Bit-Genie), [BlackMarlin](https://github.com/dsekercioglu/blackmarlin), [Demolito](https://github.com/lucasart/Demolito), [Drofa](https://github.com/justNo4b/Drofa), [Ethereal](https://github.com/AndyGrant/Ethereal), [FabChess](https://github.com/fabianvdW/FabChess), [Halogen](https://github.com/KierenP/Halogen), [Igel](https://github.com/vshcherbyna/igel), [Koivisto](https://github.com/Luecx/Koivisto), [Laser](https://github.com/jeffreyan11/laser-chess-engine), [RubiChess](https://github.com/Matthies/RubiChess), [Seer](https://github.com/connormcmonigle/seer-nnue), [Stash](https://github.com/mhouppin/stash-bot), [Weiss](https://github.com/TerjeKir/weiss), [Winter](https://github.com/rosenthj/Winter), and [Zahak](https://github.com/amanjpro/zahak). A dozen or more engines are using their own private, local instances of OpenBench.

You can join OpenBench's [Discord server](https://discord.com/invite/9MVg7fBTpM) to join the discussion, see what developers are working on and talking about, or to find out how you can contribute to the project and become a part of it. OpenBench is heavily inspired by [Fishtest](https://github.com/glinscott/fishtest). The project is powered by the [Django Web Framework](https://www.djangoproject.com/) and [fastchess](https://github.com/Disservin/fastchess).

Documentation for OpenBench is available in the [Wiki](https://github.com/AndyGrant/OpenBench/wiki)

## This fork

SnowballSH/OpenBench runs the testing server for the [Avalanche](https://github.com/SnowballSH/Avalanche) chess engine. It keeps upstream's Client and wire protocol unchanged, so any OpenBench Client of the same version works against it, and adds:

- **Insights**: per-workload progress history, time left, rates, Elo with intervals, LOS and per-machine and per-CPU contributions, drawn as charts ([docs/INSIGHTS.md](docs/INSIGHTS.md)); time taken and time left on every listing.
- **Engine progress** (`/progress/`): Elo gained from greens, weekly SPRT outcomes and games per day, per engine.
- **Compare** two workloads side by side, **Clone** a workload into a prefilled form, and export a workload's history as CSV.
- **Fleet pages**: machines online and recently offline, per-machine history, per-user activity; a manager-only storage overview.
- **Security**: POST-only state changes with CSRF, a strict Content-Security-Policy, login throttling, worker report ownership and payload checks ([docs/SECURITY.md](docs/SECURITY.md)).
- **A refreshed interface** with light and dark themes, a responsive layout and axe-clean accessibility ([docs/UI.md](docs/UI.md)).
- **An HTTP API** with meaningful status codes ([docs/API.md](docs/API.md)), and a container image published by CI ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).

Tests, ruff, mypy strict and the boundary between fork-owned and upstream code are described in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
