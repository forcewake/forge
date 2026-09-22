mkdir -p @@DEST@@
if [ -n "$@@ENV_VAR@@" ]; then
  printf "%s" "$@@ENV_VAR@@" > @@DEST@@/auth.json
  chmod 600 @@DEST@@/auth.json
fi
