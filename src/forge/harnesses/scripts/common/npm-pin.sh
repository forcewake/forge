for attempt in 1 2 3; do
  npm install -g --no-fund --no-audit @@PACKAGE@@@@PIN@@ && break
  echo "npm install of @@CLI@@ failed (attempt $attempt), retrying..."
  sleep $((attempt * 5))
done
