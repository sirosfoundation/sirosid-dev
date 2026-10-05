# The sirosid shared edge: nginx answering instance hosts with fly-replay and
# serving the apex's static site (sirosid_core/edge.py). scripts/edge-up.py
# assembles the build context - edge.conf generated for the domain, site/ with
# the console URL filled in - so build it from such a context, not from edge/.
FROM nginx:1.29-alpine
RUN rm -f /etc/nginx/conf.d/*.conf
COPY edge.conf /etc/nginx/conf.d/edge.conf
COPY site/ /srv/site/
RUN nginx -t
EXPOSE 8080
