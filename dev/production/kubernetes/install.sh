#!/usr/bin/env bash
# Install k3s with Kata Containers for Director's hosted sites, alongside the existing
# Docker Compose stack. Usage, on the server, from this directory:
#   sudo ./install.sh
# Safe to rerun. It changes no running site: sites move only after the Orchestrator is
# switched to the Kubernetes runtime and redeployed (see DEPLOYMENT.md).
set -euo pipefail

K3S_VERSION=${K3S_VERSION:-v1.33.5+k3s1}
KATA_VERSION=${KATA_VERSION:-3.21.0}
TRAEFIK_VERSION=${TRAEFIK_VERSION:-v3.7.13}
PRODUCTION=${PRODUCTION:-/opt/director5-production}
NAMESPACE=director-sites

cd "$(dirname "${BASH_SOURCE[0]}")"
[[ $(id -u) == 0 ]] || { echo 'Run as root: sudo ./install.sh' >&2; exit 1; }
[[ -c /dev/kvm ]] || { echo 'Kata Containers needs /dev/kvm (hardware virtualization).' >&2; exit 1; }
[[ -f $PRODUCTION/state/installation.json ]] || {
    echo "No Director installation at $PRODUCTION." >&2
    exit 1
}
for command in curl docker python3; do
    command -v "$command" >/dev/null || { echo "Install $command first." >&2; exit 1; }
done

echo '==> k3s'
install -d -m 0755 /etc/rancher/k3s
# Site images are pushed to the registry in compose.kubernetes.yaml on 127.0.0.1:5000.
cat >/etc/rancher/k3s/registries.yaml <<'YAML'
mirrors:
  "127.0.0.1:5000":
    endpoint:
      - "http://127.0.0.1:5000"
YAML
if ! command -v k3s >/dev/null || [[ $(k3s --version | head -1) != *"$K3S_VERSION"* ]]; then
    # Traefik and the service load balancer stay in Docker Compose, which already owns
    # ports 80 and 443. The Compose containers reach the API as host.docker.internal.
    curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" sh -s - server \
        --disable traefik \
        --disable servicelb \
        --tls-san host.docker.internal \
        --write-kubeconfig-mode 600
fi
systemctl restart k3s
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl() { k3s kubectl "$@"; }
# The node registers a few seconds after k3s starts; waiting on zero nodes fails at once.
for _ in $(seq 90); do
    [[ -n $(kubectl get nodes -o name 2>/dev/null) ]] && break
    sleep 2
done
kubectl wait --for=condition=Ready node --all --timeout=180s

echo '==> Firewall: keep the Kubernetes API and kubelet off the public interface'
public_interface=$(ip -o route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "dev") print $(i + 1)}')
# In the unit, \$\$ becomes systemd's escape for a literal $.
cat >/etc/systemd/system/director-kubernetes-firewall.service <<UNIT
[Unit]
Description=Block public access to Director's Kubernetes control ports
After=network-online.target k3s.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for port in 6443 10250; do iptables -C INPUT -i $public_interface -p tcp --dport \$\$port -j DROP 2>/dev/null || iptables -I INPUT -i $public_interface -p tcp --dport \$\$port -j DROP; done'

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now director-kubernetes-firewall.service
systemctl restart director-kubernetes-firewall.service

echo '==> Kata Containers'
if ! command -v helm >/dev/null; then
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi
helm upgrade --install kata-deploy \
    oci://ghcr.io/kata-containers/kata-deploy-charts/kata-deploy \
    --version "$KATA_VERSION" \
    --namespace kube-system \
    --set k8sDistribution=k3s \
    --wait --timeout 10m
# kata-deploy's node agent creates the runtime classes once it has installed Kata,
# which can be after Helm reports the release as deployed.
runtime_class=
for _ in $(seq 60); do
    classes=$(kubectl get runtimeclass -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep '^kata' || true)
    for candidate in kata kata-qemu kata-clh $classes; do
        if grep -qx "$candidate" <<<"$classes"; then
            runtime_class=$candidate
            break 2
        fi
    done
    sleep 5
done
if [[ -z $runtime_class ]]; then
    echo 'kata-deploy created no Kata runtime class. Its node agent reported:' >&2
    kubectl get runtimeclass >&2 || true
    kubectl -n kube-system get pods -o wide | grep -i kata >&2 || true
    kubectl -n kube-system logs daemonset/kata-deploy --tail=40 >&2 || true
    exit 1
fi
echo "Using runtime class $runtime_class"

# A Kata pod boots its own guest kernel, so its kernel release differs from the host's.
kubectl delete pod director-kata-check --ignore-not-found --wait=true >/dev/null
kubectl run director-kata-check --image=busybox:1.37 --restart=Never \
    --overrides="{\"spec\":{\"runtimeClassName\":\"$runtime_class\"}}" -- uname -r
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/director-kata-check --timeout=180s
guest_kernel=$(kubectl logs director-kata-check)
kubectl delete pod director-kata-check --wait=false >/dev/null
if [[ $guest_kernel == "$(uname -r)" ]]; then
    echo "The check pod ran on the host kernel ($guest_kernel), not in a Kata VM." >&2
    exit 1
fi
echo "Kata VM kernel $guest_kernel (host $(uname -r))"

echo '==> Traefik routes and Director access'
kubectl apply --server-side --force-conflicts -f \
    "https://raw.githubusercontent.com/traefik/traefik/$TRAEFIK_VERSION/docs/content/reference/dynamic-configuration/kubernetes-crd-definition-v1.yml"
kubectl apply -f director.yaml
docker_gateway=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
[[ $docker_gateway =~ ^[0-9.]+$ ]] || { echo 'Could not find the Docker bridge address.' >&2; exit 1; }
sed "s/DOCKER_GATEWAY/$docker_gateway/" database-services.yaml | kubectl apply -f -
for account in orchestrator traefik; do
    secret=director-$account-token
    for _ in $(seq 30); do
        token=$(kubectl -n "$NAMESPACE" get secret "$secret" -o jsonpath='{.data.token}')
        [[ -n $token ]] && break
        sleep 1
    done
    [[ -n $token ]] || { echo "Kubernetes did not issue $secret." >&2; exit 1; }
    target=$PRODUCTION/state/kubernetes/$account
    install -d -m 0700 "$target"
    (
        umask 077
        printf '%s' "$token" | base64 -d >"$target/token"
        kubectl -n "$NAMESPACE" get secret "$secret" -o jsonpath='{.data.ca\.crt}' | base64 -d >"$target/ca.crt"
        printf '%s' "$NAMESPACE" >"$target/namespace"
    )
done
install -d -m 0755 "$PRODUCTION/state/registry"
install -m 0644 ../compose.kubernetes.yaml "$PRODUCTION/compose.kubernetes.yaml"

python3 - "$PRODUCTION/.env" "$runtime_class" "$docker_gateway" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
values = {"KUBERNETES_RUNTIME_CLASS": sys.argv[2], "DIRECTOR_DOCKER_GATEWAY": sys.argv[3]}
lines = [line for line in path.read_text().splitlines() if line.split("=", 1)[0] not in values]
lines += [f"{key}={value}" for key, value in values.items()]
path.write_text("\n".join(lines) + "\n")
PY

cat <<EOF

k3s and Kata are ready. No site has moved yet. To switch Director to Kubernetes:

  cd $PRODUCTION
  echo 'COMPOSE_FILE=compose.yaml:compose.kubernetes.yaml' >> .env
  docker compose --env-file .env up -d registry site-postgres mysql orchestrator traefik
  docker compose --env-file .env exec manager python manage.py redeploy_sites <site id>

Check that site, then run redeploy_sites with no ids to move the rest.
EOF
