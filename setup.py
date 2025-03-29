from setuptools import setup, find_packages

setup(
    name="inventree-websocket-plugin",
    version="0.1.0",
    description="WebSocket server plugin for InvenTree",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="benoit505",
    author_email="billiaubenoit@gmail.com",
    url="https://github.com/benoit505/inventree-websocket-plugin",
    packages=find_packages(),
    install_requires=[
        "websockets>=10.0",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Framework :: Django",
        "Topic :: System :: Networking",
    ],
    python_requires=">=3.6",
)
