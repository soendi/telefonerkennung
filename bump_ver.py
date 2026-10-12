with open('telefonerkennung.py', 'rb') as f:
    content = f.read()
content = content.replace(b'APP_VERSION = "3.1.15"', b'APP_VERSION = "3.1.17"')
with open('telefonerkennung.py', 'wb') as f:
    f.write(content)
print('Done')