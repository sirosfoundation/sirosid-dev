# Mock AuthZEN Trust PDP Dockerfile
FROM node:20-alpine

WORKDIR /app

# Install wget for health checks
RUN apk add --no-cache wget

# Copy source files
COPY package.json ./
COPY index.mjs ./

# No npm install needed - uses only built-in Node.js modules

EXPOSE 9091

CMD ["node", "index.mjs"]
