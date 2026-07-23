FROM node:18-slim AS builder

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .
RUN npm run build

# --- Production stage ---
FROM nginx:alpine

COPY --from=builder /app/dist /usr/share/nginx/html

# Nginx config: SPA fallback + API proxy
RUN cat > /etc/nginx/conf.d/default.conf <<'EOF'
server {
    listen 3000;
    server_name _;

    location / {
        root /usr/share/nginx/html;
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://core:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF

EXPOSE 3000

CMD ["nginx", "-g", "daemon off;"]
