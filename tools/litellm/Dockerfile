FROM ghcr.io/berriai/litellm:v1.106.2@sha256:7294b90f868c5c38c99b83021c7ba46f9b439e95f8e32964c925c7d2563a03f7

ARG FORGE_GATEWAY_POLICY=tools/litellm/forge-budget-policy.json
ARG FORGE_GATEWAY_CONFIG=tools/litellm/config.yaml
COPY tools/litellm/guard_policy.py tools/litellm/guard_ledger.py tools/litellm/forge_budget_guard.py tools/litellm/bootstrap.py tools/litellm/build_manifest.py /opt/forge-budget/
COPY tools/litellm/Dockerfile /opt/forge-budget/Dockerfile
COPY ${FORGE_GATEWAY_POLICY} /opt/forge-budget/forge-budget-policy.json
COPY ${FORGE_GATEWAY_CONFIG} /opt/forge-budget/config.yaml
RUN python -I /opt/forge-budget/build_manifest.py && chmod -R a-w /opt/forge-budget

ENTRYPOINT ["python", "-I", "/opt/forge-budget/bootstrap.py"]
CMD ["--host", "0.0.0.0", "--port", "4000"]
