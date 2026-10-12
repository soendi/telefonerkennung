with open('telefonerkennung.py', 'rb') as f:
    content = f.read()
content = content.replace(b'APP_VERSION = "3.1.26"', b'APP_VERSION = "3.2.0"')
with open('telefonerkennung.py', 'wb') as f:
    f.write(content)
print('Done')