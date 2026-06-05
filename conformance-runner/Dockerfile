FROM mcr.microsoft.com/playwright:v1.52.0-noble

WORKDIR /app
COPY package.json ./
RUN npm install --production
COPY . .

ENV NODE_TLS_REJECT_UNAUTHORIZED=0
EXPOSE 3001

CMD ["node", "server.mjs"]
