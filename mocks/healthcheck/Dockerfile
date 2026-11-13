# Static HTTP healthcheck for containers without a shell or curl (the SUNET/vc
# distroless runtime). The image only exists to copy the binary into a shared
# volume; see the vc-healthcheck service in docker-compose.vc-services.yml.
FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 go build -ldflags "-s -w" -o /healthcheck .

FROM busybox:1.37
COPY --from=build /healthcheck /healthcheck
