---
license: openrail++
library_name: diffusers
base_model: stabilityai/stable-diffusion-xl-base-1.0
tags:
  - lora
  - text-to-image
  - stable-diffusion-xl
  - sdxl
  - sdxl-1.0
pipeline_tag: text-to-image
---

# SDXL_Sacred_beast神兽

![preview](./preview.jpg)

**Base model**: SDXL 1.0
**Trained words**: BJ_Sacred_beast

## 🧠 Usage (Python)

🔑 **Get your MUAPI key** from [muapi.ai/access-keys](https://muapi.ai/access-keys)

```python
import requests, os
url = "https://api.muapi.ai/api/v1/sdxl-lora-image"
headers = {"Content-Type": "application/json", "x-api-key": os.getenv("MUAPIAPP_API_KEY")}
payload = {"prompt": "masterpiece, best quality", "lora_model": "sdxl_sacred_beast神兽"}
print(requests.post(url, headers=headers, json=payload).json())
```
