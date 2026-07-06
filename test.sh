cat <<EOF >/tmp/test.py
with open('/workspace/test.txt', 'w') as f:
  f.write('Hello, world!')
EOF
python3 /tmp/test.py
cat /workspace/test.txt