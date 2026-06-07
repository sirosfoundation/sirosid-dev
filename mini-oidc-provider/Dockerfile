FROM node:22-slim
WORKDIR /app
COPY package.json .
RUN npm install --omit=dev
COPY server.mjs .
EXPOSE 9005
CMD ["node", "server.mjs"]
