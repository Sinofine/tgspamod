{ pkgs }:
let
  ps = pkgs.python3Packages;
  telethon = ps.buildPythonPackage {
    pname = "telethon";
    version = "1.45.0";
    # Upstream sdist references hatch_build.py but omits it.
    # Use the upstream pure-Python wheel from the same release.
    format = "wheel";
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/92/ac/241c09e6905215f225d8088438ceb1f32cc4b4865f3f49068cb402debfd7/telethon-1.45.0-py3-none-any.whl";
      hash = "sha256-M54kyDrtyfEsvjiSZGJ+3El3nkKJxTWqkKr8TOYveXI=";
    };
    dependencies = [ ps.pyaes ps.rsa ];
    pythonImportsCheck = [ "telethon" "telethon.tl.types" "telethon.tl.functions" ];
  };
  httpx = ps.buildPythonPackage {
    pname = "httpx";
    version = "0.28.1";
    pyproject = true;
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/b1/df/48c586a5fe32a0f01324ee087459e112ebb7224f646c0b5023f5e79e9956/httpx-0.28.1.tar.gz";
      hash = "sha256-demMXxaw81tWeFb1l/Bv8icKN0RwpcI5IkJSjj4+Qvw=";
    };
    build-system = [ ps.hatchling ps.hatch-fancy-pypi-readme ];
    dependencies = [ ps.anyio ps.certifi ps.httpcore ps.idna ];
    pythonImportsCheck = [ "httpx" ];
  };
  dotenv = ps.buildPythonPackage {
    pname = "python-dotenv";
    version = "1.2.3";
    pyproject = true;
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/6a/53/ed9d74092561d4b01a2ef1349d52cdbc135e526c245f366b089cfca6de49/python_dotenv-1.2.3.tar.gz";
      hash = "sha256-ogpZTavqo4VyWqI51SRIccFD7LNWrdiiD88jdzpsOjU=";
    };
    build-system = [ ps.setuptools ];
    pythonImportsCheck = [ "dotenv" ];
  };
  socks = ps.buildPythonPackage {
    pname = "python-socks";
    version = "3.1.1";
    pyproject = true;
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/04/ad/484ffb79532517b11a90af38647c38652224650b31a7ae1cedd5a418d8ab/python_socks-3.1.1.tar.gz";
      hash = "sha256-jT6BfNvoWNwLuMj9yOebbON6zOEQ0zN0xvV6Z1zJAp4=";
    };
    build-system = [ ps.setuptools ];
    pythonImportsCheck = [ "python_socks.async_.asyncio" ];
  };
in
pkgs.python3.withPackages (_: [ telethon httpx dotenv socks ])
