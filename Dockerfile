# Thin root wrapper so Glama can build from repo root.
FROM node:22-alpine
WORKDIR /app
COPY mcp/package.json ./
COPY mcp/meld-mcp.mjs ./
ENV MELD_BASE=https://meld.mergeinc.workers.dev
ENV NODE_ENV=production
CMD ["node", "meld-mcp.mjs"]
