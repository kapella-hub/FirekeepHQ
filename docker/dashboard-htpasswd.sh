#!/bin/sh
# Basic-auth bootstrap for the dashboard image. Runs before the stock
# 20-envsubst-on-templates.sh renders /etc/nginx/templates/.
#
# DASHBOARD_HTPASSWD holds htpasswd-format line(s) ("user:$apr1$...").
# Unset/empty => the auth_basic directives are commented out of the template
# instead: the office cluster fronts the dashboard with its ingress host, and
# an empty .htpasswd would lock everyone out rather than open the door.
set -e
TEMPLATE=/etc/nginx/templates/default.conf.template
if [ -n "${DASHBOARD_HTPASSWD:-}" ]; then
  printf '%s\n' "${DASHBOARD_HTPASSWD}" > /etc/nginx/.htpasswd
  chmod 0644 /etc/nginx/.htpasswd
else
  # Matches both auth_basic and auth_basic_user_file.
  sed -i 's|^\( *\)auth_basic|\1# auth_basic|' "$TEMPLATE"
fi
