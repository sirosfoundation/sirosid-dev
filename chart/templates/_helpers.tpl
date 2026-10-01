{{- define "siros-id.namespace" -}}
{{- if .Values.tenant.namespace }}
{{- .Values.tenant.namespace }}
{{- else }}
{{- .Release.Namespace }}
{{- end }}
{{- end }}

{{/*
Define the hostnames for the tenant
*/}}
{{ $_ := required "Domain root must be set" .Values.domain.root }}
{{- define "siros-id.hostname.issuerRegistry" -}}
{{- .Values.hostnames.issuerRegistry | default (printf "%s.issuer-registry.%s" .Values.tenant.id .Values.domain.root) -}}
{{- end -}}
{{- define "siros-id.hostname.issuer" -}}
{{- .Values.hostnames.issuer | default (printf "%s.issuer.%s" .Values.tenant.id .Values.domain.root) -}}
{{- end -}}
{{- define "siros-id.hostname.verifier" -}}
{{- .Values.hostnames.verifier | default (printf "%s.verifier.%s" .Values.tenant.id .Values.domain.root) -}}
{{- end -}}
{{- define "siros-id.hostname.walletBackend" -}}
{{- .Values.hostnames.walletBackend | default (printf "backend.%s" .Values.domain.root) -}}
{{- end -}}
{{- define "siros-id.hostname.walletFrontend" -}}
{{- .Values.hostnames.walletFrontend | default (.Values.domain.root) -}}
{{- end -}}
{{- define "siros-id.hostname.walletFrontendBasePath" -}}
{{- .Values.walletFrontend.basePath | default (printf "/id/%s" .Values.tenant.id) -}}
{{- end -}}

{{- define "siros-id.origins.walletFrontend" -}}
https://{{- .Values.hostnames.walletFrontend | default (.Values.domain.root) -}}
{{- end -}}

{{- define "siros-id.labels" -}}
{{- $root := index . 0 -}}
{{- $params := index . 1 -}}
{{- $commonLabels := mustDeepCopy $root.Values.global.commonLabels -}}
{{- $labels := mustDeepCopy $params | mergeOverwrite $commonLabels -}}
labels:
  app.kubernetes.io/managed-by: {{ $root.Release.Service | quote }}
  app.kubernetes.io/instance: {{ $root.Release.Name | quote }}
  app.kubernetes.io/part-of: "siros-id"
  {{- with $labels -}}
  {{- toYamlPretty . | nindent 2 }}
  {{- end -}}
{{- end -}}

{{/* Annotations template
Call with (list . (dict "name" "example"))
*/}}
{{- define "siros-id.annotations" -}}
{{- $root := index . 0 -}}
{{- $params := index . 1 -}}
{{- $commonAnnotations := mustDeepCopy $root.Values.global.commonAnnotations -}}
{{- $annotations := mustDeepCopy $params | mergeOverwrite $commonAnnotations -}}
{{- with $annotations -}}
annotations: {{- toYamlPretty . | nindent 2 }}
{{- end -}}
{{- end -}}

{{- define "siros-id.displayName" -}}
{{- .Values.tenant.displayName | default (.Values.tenant.id) | quote -}}
{{- end -}}

{{- define "siros-id.originFromUrl" -}}
{{- $url := . -}}
{{- $urlParsed := urlParse $url -}}
{{- urlJoin (dict "scheme" $urlParsed.scheme "host" $urlParsed.host) -}}
{{- end -}}

{{- define "siros-id.dataOrFile" -}}
{{- $root := index . 0 -}}
{{- $params := index . 1 -}}
{{- if $params.data -}}
{{ $params.data }}
{{- else if $params.file -}}
{{ $root.Files.Get $params.file }}
{{- else -}}
{{ fail "Object must contain non-empty .data or .file property" }}
{{- end -}}
{{- end -}}

{{/*
Render one vc service's config.yaml, deep-merging its extraConfig escape hatch
in last.

Usage:
  {{- include "siros-id.vc.renderConfig" (list . "siros-id.verifier.config" .Values.verifier.extraConfig) | nindent 4 }}

Every field this chart models has a real value of its own; extraConfig exists
for the rest of vc's config surface (pkg/model/config.go is considerably wider
than what a tenant normally needs, and it moves faster than this chart's
release cadence). It is a plain deep merge over the rendered result, so it can
set a field the chart doesn't know about *or* override one it does.

TWO THINGS TO GET RIGHT:

1. extraConfig is rooted at the WHOLE config file, not at the service's own
   section - the same way vc's model.Cfg is (Common + APIGW/Issuer/Verifier/
   Registry). So it needs the section wrapper, and can also reach `common`:

     verifier:
       extraConfig:
         verifier:                    # <- the section, not omitted
           credential_display:
             enable: true
         common:                      # <- reachable too
           log:
             level: debug

   Omitting the wrapper silently writes a top-level key vc ignores, since vc
   unmarshals its config non-strictly and never errors on an unknown field.

2. Merge semantics are helm's `mergeOverwrite`: maps merge recursively, but a
   LIST REPLACES the list it merges over rather than appending to it.
   Overriding one entry of e.g. inbound.openid4vp.supported_credentials
   therefore means restating the whole list.
*/}}
{{- define "siros-id.vc.renderConfig" -}}
{{- $root := index . 0 -}}
{{- $template := index . 1 -}}
{{- $extra := default dict (index . 2) -}}
{{- $config := include $template $root | fromYaml -}}
{{- if $config.Error -}}
{{- fail (printf "template %s did not render parseable YAML: %s" $template $config.Error) -}}
{{- end -}}
{{- toYamlPretty (mergeOverwrite $config (deepCopy $extra)) -}}
{{- end -}}

{{/*
common.credential_metadata - the credential-constructor map, keyed by OAuth2
scope, shared by issuer-apigw, issuer-core and the verifier.

vc's model.CredentialMetadata requires exactly one of vctm_file_path/vctm_url/
mddl_file_path/mddl_url/vct+doctype. SD-JWT types (format dc+sd-jwt) carry a
VCTM; ISO 18013-5 mdoc types (format mso_mdoc) carry an MDDL schema instead,
and pointing an mdoc type at a vctm_file_path fails validation at startup. Both
are rendered into the same `vctms` ConfigMap (templates/03-cred-common.yaml) -
the file extension is what distinguishes them on disk.
*/}}
{{- define "siros-id.vc.credentialMetadata" -}}
{{- $_ := required "You must define credential types in the value features.credentialTypes" .Values.features.credentialTypes -}}
{{- range $id, $data := .Values.features.credentialTypes }}
{{ $id | quote }}:
  {{- if $data.mdocSchema }}
  mddl_file_path: /vctms/{{ $id }}.mdoc.json
  {{- else if $data.mddlUrl }}
  mddl_url: {{ $data.mddlUrl | quote }}
  {{- else if $data.vctmUrl }}
  vctm_url: {{ $data.vctmUrl | quote }}
  {{- else }}
  vctm_file_path: /vctms/{{ $id }}.json
  {{- end }}
  {{- with $data.doctype }}
  doctype: {{ . | quote }}
  {{- end }}
  format: {{ $data.format }}
  {{- with $data.disclosurePolicy }}
  disclosure_policy: {{- toYamlPretty . | nindent 4 }}
  {{- end }}
{{- end }}
{{- end -}}

{{/*
trust.wallet_attestation, shared by the verifier and issuer-apigw.

Lets a wallet authenticate with a provider-signed attestation JWT instead of
being pre-registered as a client; the PDP validates the wallet provider.
`mode` pins which WIA trust model is accepted - "etsi" (require x5c, identity
anchored in the Trusted List for Wallet Providers) or "ietf" (require iss, no
x5c, resolved via JWKS discovery). Leaving it empty accepts either, which
means a deployment expecting only ARF-conformant wallets would still accept a
JWKS-discovered one - so pin it deliberately.

Usage: {{- include "siros-id.vc.config.walletAttestation" .Values.verifier.walletAttestation | nindent 6 }}
*/}}
{{- define "siros-id.vc.config.walletAttestation" -}}
enabled: true
{{- with .mode }}
mode: {{ . | quote }}
{{- end }}
{{- with .policy }}
policy: {{- toYamlPretty . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* The list of supported credential scopes
*/}}
{{- define "siros-id.vc.credentialScopes" -}}
{{- range $id, $_ := .Values.features.credentialTypes }}
- {{ $id | quote }}
{{- end }}
{{- end -}}

{{/*
Credential types with issuance.source "assertion", as a JSON object.
Use with: {{- $types := include "siros-id.vc.credentialTypesBySource" (list . "assertion") | fromJson -}}
Credential types without an explicit source default to "datastore" and are excluded.
*/}}
{{- define "siros-id.vc.credentialTypesBySource" -}}
{{- $root := index . 0 -}}
{{- $sourceType := index . 1 -}}
{{- $result := dict -}}
{{- range $id, $data := $root.Values.features.credentialTypes -}}
{{- $_ := required "Credential type must have an issuance.source" $data.issuance.source }}
{{- if eq $data.issuance.source $sourceType -}}
{{- $_ := set $result $id $data -}}
{{- end }}
{{- end }}
{{- $result | toYaml -}}
{{- end -}}

{{- define "siros-id.branding.logoDataUrl" -}}
{{-
  .Values.features.branding.logoDataUrl
  | default (printf "data:image/png;base64,%s" (.Files.Get "config/default_logo.png" | b64enc))
-}}
{{- end -}}

{{- define "siros-id.branding.faviconDataUrl" -}}
{{-
  .Values.features.branding.faviconDataUrl
  | default (printf "data:image/png;base64,%s" (.Files.Get "config/default_favicon.png" | b64enc))
-}}
{{- end -}}

{{- define "siros-id.mongoUri" -}}
{{- $root := index . 0 -}}
{{- $database := index . 1 -}}
mongodb+srv://mongodb-svc.{{ include "siros-id.namespace" $root }}.svc.{{ $root.Values.global.clusterDomain }}/{{ $database }}?replicaSet=mongodb&ssl=true&authMechanism=MONGODB-X509
{{- end -}}

{{/* Mongo client config fragment for the service config files.
Call with (list . "database_name").
Expects the client certificate to be mounted at /client-cert.
*/}}
{{- define "siros-id.vc.config.mongo" -}}
{{- $root := index . 0 -}}
{{- $database := index . 1 -}}
mongo:
  uri: {{ include "siros-id.mongoUri" (list $root $database) }}
  tls: true
  ca_file_path: /client-cert/ca.crt
  cert_file_path: /client-cert/tls.crt
  key_file_path: /client-cert/tls.key
{{- end -}}

{{/* Tracing config fragment for the service config files.
Call with the root context.
*/}}
{{- define "siros-id.vc.config.tracing" -}}
tracing:
  enable: {{ .Values.features.otlpCollector }}
  addr: otlp-collector:4318
{{- end -}}

{{/* mTLS gRPC client config fragment for the service config files.
Call with the name of the target service, e.g. (include ... "issuer-registry").
Expects the client certificate to be mounted at /client-cert.
*/}}
{{- define "siros-id.vc.config.grpcClient" -}}
addr: {{ . }}:8090
tls: true
server_name: {{ . }}
ca_file_path: /client-cert/ca.crt
cert_file_path: /client-cert/tls.crt
key_file_path: /client-cert/tls.key
{{- end -}}

{{/* issuer/verifier cert chain-gen init container template
*/}}
{{- define "siros-id.vc.config.apiAuth" -}}
{{- if not (or .Values.issuer.apiAuth.jwks.enabled .Values.issuer.apiAuth.oidc.enabled) -}}
{{- fail "issuer.apiAuth.jwks.enabled or issuer.apiAuth.oidc.enabled is required" -}}
{{- end -}}
api_auth:
  rules:
    - "(vc (service *)(method *)(path /api/v1/*)(subject admin@{{ .Values.tenant.id }})(authentic_source *)(scope *))"
{{- if .Values.issuer.apiAuth.jwks.enabled }}
  jwks:
    enable: true
    issuer: {{ .Values.issuer.apiAuth.jwks.issuer | quote }}
    audience: https://{{ include "siros-id.hostname.issuer" . }}
    jwks_file_path: /main-config/api_auth_jwks.json
{{ else if .Values.issuer.apiAuth.oidc.enabled }}
  oidc:
    enable: true
    issuer_url: {{ .Values.issuer.apiAuth.oidc.issuerUrl | quote }}
    client_id: {{ .Values.issuer.apiAuth.oidc.clientId | quote }}
    redirect_uri: {{ .Values.issuer.apiAuth.oidc.redirectUri | quote }}
    scopes: {{- toYamlPretty .Values.issuer.apiAuth.oidc.scopes | nindent 4 }}
{{- end -}}
{{- end -}}
