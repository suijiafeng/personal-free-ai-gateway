proxy_pass http://gateway_backend;
proxy_http_version 1.1;
proxy_set_header Host gateway;
proxy_set_header Connection "";
proxy_set_header Authorization $http_authorization;
# No caller-supplied header can turn a consumer route into an admin route.
proxy_set_header X-Forwarded-For "";
proxy_set_header Forwarded "";
proxy_set_header X-Admin "";
proxy_set_header X-Gateway-Admin "";
proxy_set_header X-Gateway-Entrance "";
proxy_buffering off;
proxy_request_buffering on;
proxy_cache off;
proxy_next_upstream off;
proxy_connect_timeout 5s;
proxy_read_timeout 95s;
proxy_send_timeout 95s;
send_timeout 25s;
