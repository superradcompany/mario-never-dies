# The guest image: dependencies only. The demo's own code is written into each machine
# when it is created (mnd/backend.py), so this file is the image's only input, and it is
# published as ghcr.io/superradcompany/mnd:deps-<first 12 hex of this file's sha256>.
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Keep Faadil's parser and Jev policy as an external, revision-pinned dependency.
# The emulator's package bundles Nintendo ROMs. They are deleted in the same layer that
# installs them, so no layer of this image carries game data; a ROM arrives at boot.
RUN pip install --no-cache-dir \
    'typesafe-mario[mario] @ git+https://github.com/fhshaik/typesafe-mario.git@ca22449ed187118d19326d1f54b01b6636578aa4' \
    && find / -xdev -name '*.nes' -delete

RUN pip install --no-cache-dir "Pillow>=11" \
    && python -c "import gym_super_mario_bros, gymnasium, nes_py, PIL, typesafe_mario.state, typesafe_sdk" \
    && test -z "$(find / -xdev -name '*.nes')"

ENV SDL_VIDEODRIVER=dummy PYTHONUNBUFFERED=1
CMD ["sleep", "infinity"]
