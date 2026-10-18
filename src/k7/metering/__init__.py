"""Usage metering: node sampler, storage sampler, spool and sinks.

K7 emits usage; it never prices it. See docs/METERING.md.
"""

# Stamped by K7Core on every sandbox Deployment and pod template at create,
# fork and restore; the node sampler reads them from the CRI pod annotations.
SANDBOX_UID_ANN = "k7.katakate.org/sandbox-uid"
VCPU_ANN = "k7.katakate.org/vcpu"
MEMORY_ANN = "k7.katakate.org/memory-bytes"
