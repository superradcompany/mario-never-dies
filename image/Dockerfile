# The guest image: dependencies only. The demo's own code is written into each machine
# when it is created (mnd/backend.py), so this file is the image's only input. CI publishes it
# from main as ghcr.io/superradcompany/mnd:latest.
#
# Two stages, because installing needs far more than running. Only the second is published.
FROM python:3.13-slim AS install

# git is only how pip reaches a pinned revision.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Keep Faadil's parser and Jev policy as an external, revision-pinned dependency.
# Two things come along that must not ship. The emulator's package bundles Nintendo ROMs: no
# game data belongs in the image, a ROM arrives at boot. And pygame and pyglet draw windows,
# which the game loop never opens: it renders frames to arrays, so they go, and with them any
# need for a GL stack in the image.
RUN pip install --no-cache-dir --prefix=/deps \
    'typesafe-mario[mario] @ git+https://github.com/fhshaik/typesafe-mario.git@ca22449ed187118d19326d1f54b01b6636578aa4' \
    "Pillow>=11" \
    && find /deps -name '*.nes' -delete \
    && rm -rf /deps/lib/python3.13/site-packages/pygame* /deps/lib/python3.13/site-packages/pyglet*

FROM python:3.13-slim
COPY --from=install /deps /usr/local

# What the game loop imports must load with nothing else installed, and no ROM may be here.
RUN python -c "import gym_super_mario_bros, gymnasium, nes_py, PIL, typesafe_mario.state, typesafe_sdk" \
    && test -z "$(find / -xdev -name '*.nes')"

ENV PYTHONUNBUFFERED=1
CMD ["sleep", "infinity"]
