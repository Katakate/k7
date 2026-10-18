# UNTESTED (see the support matrix). Bakes K7 node prep into an EKS
# AL2023 AMI by running the k7-node-installer image in `bake` mode: the same
# role as the DaemonSet, minus the `first-boot` disk state (k7d XFS image,
# kfd thin pool) and node labels — the DaemonSet does both when the node
# joins. The only promise is that it runs the same role. Known gap: the role
# installs packages with apt, so an AL2023 bake fails until the distro adapter
# gains a dnf path.
packer {
  required_plugins {
    amazon = {
      version = "= 1.3.9"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

variable "region" {
  type = string
}

variable "source_ami" {
  type        = string
  description = "EKS-optimized AL2023 AMI id for your Kubernetes version (pinned, no lookup)."
}

variable "instance_type" {
  type        = string
  description = "Builder instance type, e.g. c8i.2xlarge (nested virt for /dev/kvm)."
}

variable "installer_image" {
  type        = string
  description = "ghcr.io/katakate/k7-node-installer@sha256:<digest>"
}

variable "installer_config" {
  type        = string
  description = "JSON in the k7-node-installer ConfigMap schema, with distro=eks-al2023."
}

source "amazon-ebs" "k7_node" {
  region        = var.region
  source_ami    = var.source_ami
  instance_type = var.instance_type
  ssh_username  = "ec2-user"
  ami_name      = "k7-node-al2023-${formatdate("YYYYMMDDhhmmss", timestamp())}"
}

build {
  sources = ["source.amazon-ebs.k7_node"]

  provisioner "shell" {
    environment_vars = [
      "INSTALLER_IMAGE=${var.installer_image}",
      "INSTALLER_CONFIG=${var.installer_config}",
    ]
    inline = [
      "set -eu",
      "sudo mkdir -p /run/k7-node-installer",
      "printf '%s' \"$INSTALLER_CONFIG\" | sudo tee /run/k7-node-installer/config.json >/dev/null",
      "sudo ctr images pull \"$INSTALLER_IMAGE\"",
      "sudo ctr run --rm --privileged --net-host --with-ns pid:/proc/1/ns/pid --cwd /opt/k7/src --mount type=bind,src=/,dst=/host,options=rbind:rw --mount type=bind,src=/run/k7-node-installer,dst=/etc/k7-node-installer,options=rbind:ro \"$INSTALLER_IMAGE\" k7-node-bake python3 -m k7.deploy.node_installer.entrypoint bake",
      "sudo rm -rf /run/k7-node-installer",
    ]
  }
}
