FROM node:22-alpine AS build
WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build
