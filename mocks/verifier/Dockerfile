# Mock Verifier Service Dockerfile
FROM node:20-alpine

WORKDIR /app

# Install wget for health checks
RUN apk add --no-cache wget

# Copy source
COPY index.ts ./
COPY package.json ./

# Install dependencies
RUN npm install

# Build TypeScript
RUN npx tsc index.ts --outDir dist --esModuleInterop --module commonjs --target es2020

EXPOSE 9001

CMD ["node", "dist/index.js"]
