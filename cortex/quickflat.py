import os
import sys
import time
import glob
import Image
import cPickle
import cStringIO
import binascii
import numpy as np

import db
import utils

def _gen_flat_mask(subject, height=1024):
    import polyutils
    import Image
    import ImageDraw
    pts, polys, norm = db.surfs.getVTK(subject, "flat", merge=True, nudge=True)
    left, right = polyutils.trace_both(pts, polys)

    pts -= pts.min(0)
    pts *= height / pts.max(0)[1]

    im = Image.new('L', (int(pts.max(0)[0]), height))
    draw = ImageDraw.Draw(im)
    draw.polygon(pts[left, :2].ravel().tolist(), fill=255)
    draw.polygon(pts[right, :2].ravel().tolist(), fill=255)
    return np.array(im) > 0

def _make_flat_cache(subject, xfmname, height=1024):
    from scipy.spatial import cKDTree

    flat, polys, norm = db.surfs.getVTK(subject, "flat", merge=True, nudge=True)
    valid = np.unique(polys)
    fmax, fmin = flat.max(0), flat.min(0)
    size = fmax - fmin
    aspect = size[0] / size[1]
    width = int(aspect * height)

    #Get the mask idx for each vertex
    cmask = utils.get_cortical_mask(subject, xfmname)
    imask = cmask.astype(np.uint32)
    imask[cmask > 0] = np.arange(cmask.sum())
    coords = np.vstack(db.surfs.getCoords(subject, xfmname))
    ridx = np.ravel_multi_index(coords.T, cmask.shape[::-1], mode='clip')
    mcoords = imask.T.ravel()[ridx[valid]]

    mask = _gen_flat_mask(subject, height=height).T
    assert mask.shape[0] == width and mask.shape[1] == height
    flatpos = np.mgrid[fmin[0]:fmax[0]:width*1j, fmin[1]:fmax[1]:height*1j].reshape(2,-1)
    kdt = cKDTree(flat[valid,:2])
    dist, idx = kdt.query(flatpos.T[mask.ravel()])

    return mcoords[idx], mask

cache = dict()
def get_cache(subject, xfmname, recache=False, height=1024):
    key = (subject, xfmname)
    if not recache and key in cache:
        return cache[key]

    cacheform = db.surfs.getFiles(subject)['flatcache']
    cachefile = cacheform.format(xfmname=xfmname, height=height, date="*")
    #pull a list of candidate cache files
    files = glob.glob(cachefile)
    if len(files) < 1 or recache:
        #if recaching, delete all existing files
        for f in files:
            os.unlink(f)
        print "Generating a flatmap cache"
        #pull points and transform from database
        coords, mask = _make_flat_cache(subject, xfmname, height=height)
        #save them into the proper file
        date = time.strftime("%Y%m%d")
        cachename = cacheform.format(xfmname=xfmname, height=height, date=date)
        cPickle.dump((coords, mask), open(cachename, "w"), 2)
    else:
        coords, mask = cPickle.load(open(files[0]))

    cache[key] = coords, mask
    return coords, mask

def make(data, subject, xfmname, recache=False, height=1024, **kwargs):
    coords, mask = get_cache(subject, xfmname, recache=recache, height=height)

    if data.ndim in (1, 3):
        data = data[np.newaxis]

    if data.ndim == 4:
        cmask = utils.get_cortical_mask(subject, xfmname)
        data = data[:, cmask]

    length = data.shape[0]
    img = np.nan*np.ones((length,)+mask.shape, dtype=data.dtype)
    img[:, mask] = data[:, coords]
    return img.reshape((length,)+mask.shape).swapaxes(1, 2)[:,::-1].squeeze()

rois = dict()
def overlay_rois(im, subject, name=None, height=1024, labels=True, **kwargs):
    import shlex
    import subprocess as sp
    from matplotlib.pylab import imsave

    if name is None:
        name = 'png:-'

    key = (subject, labels)
    if key not in rois:
        print "loading %s"%subject
        rois[key] = utils.get_roipack(subject).get_texture(height, labels=labels)
    cmd = "composite {rois} - {name}".format(rois=rois[key].name, name=name)
    proc = sp.Popen(shlex.split(cmd), stdin=sp.PIPE, stdout=sp.PIPE)

    fp = cStringIO.StringIO()
    imsave(fp, im, **kwargs)
    fp.seek(0)
    out, err = proc.communicate(fp.read())
    if len(out) > 0:
        fp = cStringIO.StringIO()
        fp.write(out)
        fp.seek(0)
        return fp

def make_png(data, subject, xfmname, name=None, with_rois=True, recache=False, height=1024, **kwargs):
    im = make(data, subject, xfmname, recache=recache, height=height)

    if with_rois:
        return overlay_rois(im, subject, name=name, height=height, **kwargs)

    if name is None:
        fp = cStringIO.StringIO()
        imsave(fp, im, **kwargs)
        fp.seek(0)
        return fp

    imsave(name, im, **kwargs)

def make_figure(data, subject, xfmname, name=None, recache=False, height=1024, with_rois=True, labels=True, colorbar=True, dpi=100, **kwargs):
    im = make(data, subject, xfmname, recache=recache, height=height)

    from matplotlib import pyplot as plt
    fig = plt.figure()
    ax = fig.add_axes((0,0,1,1))
    cimg = ax.imshow(im, aspect='equal', **kwargs)
    ax.axis('off')

    if colorbar:
        cbar = fig.add_axes((.4, .07, .2, .05))
        fig.colorbar(cimg, cax=cbar, orientation='horizontal')

    if with_rois:
        rois = utils.get_roipack(subject)
        overlay = plt.imread(rois.get_texture(height, labels=labels))
        oax = fig.add_axes((0,0,1,1))
        oimg = oax.imshow(overlay, aspect='equal', interpolation='nearest')

    fig.set_dpi(dpi)
    fig.set_size_inches(np.array(im.shape)[::-1] / float(dpi))

    if name is None:
        return fig
    
    fig.savefig(name, transparent=True)
    plt.close()

def make_movie(name, data, subject, xfmname, with_rois=True, tr=2, interp='linear', fps=30, vcodec='libtheora', bitrate="8000k", vmin=None, vmax=None, **kwargs):
    import shlex
    import shutil
    import tempfile
    import subprocess as sp
    import multiprocessing as mp
    
    from scipy.interpolate import interp1d

    path = tempfile.mkdtemp()
    impath = os.path.join(path, "im%09d.png")
    ims = make(data, subject, xfmname, **kwargs)
    times = np.arange(0, len(ims)*tr, tr)
    interp = interp1d(times, ims, kind=interp, axis=0, copy=False)

    if vmin is None:
        vmin = ims.min()
    if vmax is None:
        vmax = ims.max()
    
    def overlay(idxts):
        idx, ts = idxts
        overlay_rois(interp(ts), subject, name=impath%idx, vmin=vmin, vmax=vmax)

    #pool = mp.Pool()
    frames = np.linspace(0, times[-1], (len(times)-1)*tr*fps+1)
    map(overlay, enumerate(frames))

    cmd = "avconv -i {path} -vcodec {vcodec} -r {fps} -b {br} {name}".format(path=impath, vcodec=vcodec, fps=fps, br=bitrate, name=name)
    sp.call(shlex.split(cmd))
    shutil.rmtree(path)

def show(data, subject, xfmname, recache=False, height=1024, with_rois=True, **kwargs):
    from matplotlib.pylab import imshow, imread, axis
    im = make(data, subject, xfmname, recache=recache, height=height)

    if with_rois:
        #split the kwargs, since imsave is only a subset of imshow
        keys = set(('vmin', 'vmax', 'cmap', 'format', 'origin', 'dpi'))
        kwsave = dict((k, kwargs[k]) for k in keys if k in kwargs)
        kwargs = dict((k, kwargs[k]) for k in set(kwargs.keys()) - keys)
        im = imread(overlay_rois(im, subject, height=height, **kwsave))

    ax = imshow(im, **kwargs)
    ax.axes.set_aspect('equal')
    axis('off')
    return ax