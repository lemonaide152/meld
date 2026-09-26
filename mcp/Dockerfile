# Glama / MCP registry: start stdio server and answer introspection.
FROM node:22-alpine
WORKDIR /app
COPY package.json ./
COPY meld-mcp.mjs ./
# Zero npm deps — package.json may still declare type module
ENV MELD_BASE=https://meld.mergeinc.workers.dev
ENV NODE_ENV=production
CMD ["node", "meld-mcp.mjs"]
