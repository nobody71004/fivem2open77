"""GTA V default vehicle colour palette (carcols.ymt indices 0..159) as sRGB.

Source: the community colour table published on the RAGE Multiplayer wiki
(https://wiki.rage.mp/index.php?title=Vehicle_Colors).
"""

_HEX = (
    "0d1116 1c1d21 32383d 454b4f 999da0 c2c4c6 979a97 637380 63625c 3c3f47 444e54 1d2129 13181f 26282a "
    "515554 151921 1e2429 333a3c 8c9095 39434d 506272 1e232f 363a3f a0a199 d3d3d3 b7bfca 778794 c00e1a "
    "da1918 b6111b a51e23 7b1a22 8e1b1f 6f1818 49111d b60f25 d44a17 c2944f f78616 cf1f21 732021 f27d20 "
    "ffc91f 9c1016 de0f18 8f1e17 a94744 b16c51 371c25 132428 122e2b 12383c 31423f 155c2d 1b6770 66b81f "
    "22383e 1d5a3f 2d423f 45594b 65867f 222e46 233155 304c7e 47578f 637ba7 394762 d6e7f1 76afbe 345e72 "
    "0b9cf1 2f2d52 282c4d 2354a1 6ea3c6 112552 1b203e 275190 608592 2446a8 4271e1 3b39e0 1f2852 253aa7 "
    "1c3551 4c5f81 58688e 74b5d8 ffcf20 fbe212 916532 e0e13d 98d223 9b8c78 503218 473f2b 221b19 653f23 "
    "775c3e ac9975 6c6b4b 402e2b a4965f 46231a 752b19 bfae7b dfd5b2 f7edd5 3a2a1b 785f33 b5a079 fffff6 "
    "eaeaea b0ab94 453831 2a282b 726c57 6a747c 354158 9ba0a8 5870a1 eae6de dfddd0 f2ad2e f9a458 83c566 "
    "f1cc40 4cc3da 4e6443 bcac8f f8b658 fcf9f1 fffffb 81844c ffffff f21f99 fdd6cd df5891 f6ae20 b0ee6e "
    "08e9fa 0a0c17 0c0d18 0e0d14 9f9e8a 621276 0b1421 11141a 6b1f7b 1e1d22 bc1917 2d362a 696748 7a6c55 "
    "c3b492 5a6352 81827f afd6e4 7a6440 7f6a48"
).split()


def gta_color(index):
    """Palette index -> (r, g, b) floats in 0..1 (sRGB), or None when out of range."""
    try:
        i = int(index)
    except (TypeError, ValueError):
        return None
    if 0 <= i < len(_HEX):
        h = _HEX[i]
        return tuple(int(h[k:k + 2], 16) / 255.0 for k in (0, 2, 4))
    return None
