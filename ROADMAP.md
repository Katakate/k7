# 🗺️ Project Roadmap

This document outlines the upcoming milestones, goals, and long-term vision for **K7**.  
It helps contributors and users understand where the project is heading.

---

## 🚀 Current Focus

Core stability and foundational runtime improvements.

- [x] Add `--disk` argument to `k7 install` to specify external disk path explicitly for thin pool provisioning, and test it (merged PR #5)  
- [x] Test if removing DNS resolution completely doesn't break functionality (to protect against DNS exfiltration) (merged PR #6)
- [ ] Add pause/resume/fork/clone support for sandboxes 
- [ ] Fix jailer functionality (known issue)
- [ ] Add multi-node support (currently single K3s node supported)



---

## 🧩 Next Goals

Broader compatibility and container integration.

- [x] Add ARM support for Linux Debian (merged PR #4, big thanks to @spullara)
- [ ] Add Docker build / run / compose capabilities in VM sandboxes (major feature!)
- [ ] Integrate Cilium networking  
- [ ] Implement Docker pull deny/whitelist  


---

## ⚙️ Future Work

Cross-platform support and continuous delivery.

- [ ] Add QEMU support (macOS ARM, GPU support)  
- [ ] Cross-node mobility of snapshots (dependent on multi-node + sandbox snapshot/resume/fork features)
- [ ] Add AppArmor integration  
- [ ] Add CI/CD and deployment tests  

---

## 🔐 Advanced Features

Security, customization, and extended runtime capabilities.

- [ ] Add TEE (Trusted Execution Environment) support  
- [ ] Add custom rootfs support (lighter, alternative images)  

---

## 💬 How to Contribute

We welcome ideas and feedback!  
If you'd like to suggest a feature or help with one listed above:
1. Open a [Discussion](https://github.com/katakate/k7/discussions) or [Issue](https://github.com/katakate/k7/issues)
2. Reference the relevant roadmap item
3. Let's collaborate on the design or implementation

---

📅 *Last updated: October 2025*
