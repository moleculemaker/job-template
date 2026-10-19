# Licensed and private content check

Complete this for every `<tool>-job` repo before its first image is pushed, and again
when the method, its dependencies or its model weights change. Record the answers in the
tool's PR. Repos and ghcr packages are public unless this check says otherwise.

- [ ] **Method code license.** What license does the method author's code carry? Does it
      permit redistribution inside a public container image? Record the SPDX identifier
      (it also goes into the tool's `license` entry in mmli-backend's `tools.yaml`).
- [ ] **Model weights and data.** Where do weights, reference databases or training data
      come from, and under what terms? Are any "research use only", gated (e.g. a Hugging
      Face model requiring acceptance), or unpublished?
- [ ] **Commercial toolkits.** Does anything require a license key or a private registry
      (e.g. ChemAxon/Marvin, OpenEye, Schrödinger)?
- [ ] **Secrets.** Does the image or repo contain tokens, keys or credentials? (It must
      not; runtime secrets are mounted by the backend.)
- [ ] **Unpublished work.** Is the method or any of its data still under embargo or
      awaiting publication?

If every answer is clear: the repo and package stay public.

If anything is restricted: make the repo and/or ghcr package private, keep the
restricted content out of the image where possible (download at runtime with a mounted
secret), and note that the backend needs an image pull secret for this tool.
