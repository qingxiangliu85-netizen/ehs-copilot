# Third-Party Notices

EHS Copilot uses third-party Python packages through their public APIs. No third-party package source code is copied into this repository.

Direct runtime dependencies and the licenses declared by the installed distributions used for the v0.1 release check:

| Package | Tested version | Declared license |
|---|---:|---|
| Streamlit | 1.62.0 | Apache-2.0 |
| LangChain | 1.3.18 | MIT |
| LangGraph | 1.x | MIT |
| langchain-community | 0.4.2 | MIT |
| langchain-text-splitters | 1.1.2 | MIT |
| langchain-huggingface | 1.2.2 | MIT |
| faiss-cpu | 1.15.0 | MIT |
| sentence-transformers | 5.7.0 | Apache-2.0 |
| pypdf | 6.16.2 | BSD-3-Clause |
| python-dotenv | 1.2.3 | BSD-3-Clause |
| openai | 2.54.0 | Apache-2.0 |

Package versions installed by future users may differ within the ranges in `requirements.txt`. Each dependency remains subject to its own license and notices. This file is informational and does not replace the license text distributed by each dependency.

The HuggingFace embedding model is downloaded at runtime and is not redistributed in this repository. Users should review the model card and applicable model license before redistribution or commercial deployment.
