from setuptools import setup

setup(
    name='django-cmd-async',
    version='2.2.0',
    packages=['commands_async'],
    url='https://github.com/alessandrohc/django-cmd-async',
    license='MIT',
    author='Alessandro Hecht',
    author_email='alessandrohc@gmail.com',
    description='Web execution of commands asynchronously.',
    include_package_data=True,
    classifiers=[
        'Programming Language :: Python',
        'Programming Language :: Python :: 2'
    ]
)
